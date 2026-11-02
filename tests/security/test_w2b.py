"""Wave 2B: processes, providers, adapters, byte-exact edits and content hashes.

SEC-006/008, PROV-002/003/004, ADP-001/002, EDIT-001, LED-001. Synthetic data only;
providers are fixtures, never the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.process import run_command
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="process groups need POSIX")


def running(marker: str) -> list[int]:
    """PIDs whose command line contains `marker` (Linux /proc)."""

    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if marker in cmdline and int(entry.name) != os.getpid():
            pids.append(int(entry.name))
    return pids


def wait_gone(marker: str, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not running(marker):
            return True
        time.sleep(0.05)
    return not running(marker)


async def approve_all(request) -> bool:
    return True


def make_registry(workspace, *, approver=approve_all):
    session = SessionLog.create(workspace.sessions_dir, label="w2b")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
        approve=approver,
    )
    return ToolRegistry(ctx), session


# --------------------------------------------------------------------------- SEC-008
@POSIX
def test_timeout_kills_the_whole_process_group() -> None:
    started = time.monotonic()
    result = run_command(["sh", "-c", "sleep 31.7101 & sleep 31.7101"], timeout=1)
    assert result.timed_out and result.returncode == 124
    assert time.monotonic() - started < 4
    assert wait_gone("31.7101"), "a grandchild survived the timeout"


@POSIX
def test_a_background_child_does_not_outlive_a_finished_command() -> None:
    result = run_command(["sh", "-c", "sleep 31.7102 & echo done"], timeout=10)
    assert result.ok and "done" in result.stdout
    assert wait_gone("31.7102")


@POSIX
def test_output_is_capped_and_the_command_is_stopped() -> None:
    code = "import sys\nfor _ in range(400):\n    sys.stdout.write('x' * 50000)\n"
    started = time.monotonic()
    result = run_command([sys.executable, "-c", code], timeout=30, max_output_bytes=100_000)
    assert result.output_truncated
    assert len(result.stdout.encode()) <= 100_000
    assert time.monotonic() - started < 10


@POSIX
def test_stderr_is_capped_too() -> None:
    code = "import sys\nfor _ in range(400):\n    sys.stderr.write('e' * 50000)\n"
    result = run_command([sys.executable, "-c", code], timeout=30, max_output_bytes=100_000)
    assert result.output_truncated and len(result.stderr.encode()) <= 100_000


@POSIX
def test_a_cancel_event_stops_a_running_command() -> None:
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    result = run_command(["sleep", "31.7103"], timeout=60, cancel=cancel)
    assert result.cancelled and time.monotonic() - started < 3
    assert wait_gone("31.7103")


@POSIX
def test_cancelling_the_tool_call_stops_the_command(workspace) -> None:
    registry, _ = make_registry(workspace)

    async def scenario() -> float:
        task = asyncio.create_task(
            registry.call("run_shell", {"argv": ["sleep", "31.7104"], "timeout_seconds": 600})
        )
        await asyncio.sleep(0.5)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 3
    assert wait_gone("31.7104")


@POSIX
def test_wait_for_really_bounds_a_running_tool(workspace) -> None:
    registry, _ = make_registry(workspace)

    async def scenario() -> float:
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                registry.call("run_shell", {"argv": ["sleep", "31.7105"], "timeout_seconds": 600}),
                timeout=0.5,
            )
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 3
    assert wait_gone("31.7105")


def test_neg_output_exit_code_and_missing_binary_are_unchanged() -> None:
    ok = run_command([sys.executable, "-c", "print('olá'); import sys; sys.exit(3)"], timeout=10)
    assert ok.returncode == 3 and ok.stdout.strip() == "olá" and not ok.timed_out
    assert run_command(["definitely-not-a-binary-xyz"]).returncode == 127


@POSIX
def test_stdin_is_closed_so_a_reading_command_does_not_hang() -> None:
    started = time.monotonic()
    result = run_command(["cat"], timeout=10)
    assert result.ok and result.stdout == "" and time.monotonic() - started < 3


# --------------------------------------------------------------------------- EDIT-001
def call(registry, name: str, **arguments):
    return asyncio.run(registry.call(name, arguments))


def edit(registry, path: str, old: str, new: str, **extra):
    return call(registry, "edit_file", path=path, old_string=old, new_string=new, **extra)


def test_edit_keeps_crlf_line_endings(workspace) -> None:
    target = workspace.root / "corpus" / "crlf.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    registry, _ = make_registry(workspace)
    assert edit(registry, "corpus/crlf.txt", "beta", "BETA").ok
    assert target.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_edit_matches_a_multiline_old_string_in_a_crlf_file(workspace) -> None:
    target = workspace.root / "corpus" / "crlf2.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    registry, _ = make_registry(workspace)
    assert edit(registry, "corpus/crlf2.txt", "one\ntwo", "ONE\nTWO\nextra").ok
    assert target.read_bytes() == b"ONE\r\nTWO\r\nextra\r\nthree\r\n"


def test_edit_replace_all_keeps_crlf(workspace) -> None:
    target = workspace.root / "corpus" / "crlf3.txt"
    target.write_bytes(b"x\r\nx\r\n")
    registry, _ = make_registry(workspace)
    assert edit(registry, "corpus/crlf3.txt", "x", "y", replace_all=True).ok
    assert target.read_bytes() == b"y\r\ny\r\n"


def test_edit_refuses_a_file_that_is_not_utf8_and_leaves_it_alone(workspace) -> None:
    target = workspace.root / "corpus" / "lat1.txt"
    target.write_bytes(b"caf\xe9 alpha\nzeta\n")
    registry, _ = make_registry(workspace)
    result = edit(registry, "corpus/lat1.txt", "zeta", "ZETA")
    assert not result.ok and "UTF-8" in result.content
    assert target.read_bytes() == b"caf\xe9 alpha\nzeta\n"
    assert b"\xef\xbf\xbd" not in target.read_bytes()


def test_edit_refuses_mixed_line_endings_and_leaves_the_file_alone(workspace) -> None:
    target = workspace.root / "corpus" / "mixed.txt"
    target.write_bytes(b"a\r\nb\nc\r\n")
    registry, _ = make_registry(workspace)
    result = edit(registry, "corpus/mixed.txt", "b", "B")
    assert not result.ok and "line ending" in result.content
    assert target.read_bytes() == b"a\r\nb\nc\r\n"


def test_edit_preserves_a_bom_non_ascii_and_a_missing_final_newline(workspace) -> None:
    target = workspace.root / "corpus" / "bom.txt"
    target.write_bytes("﻿çafé 日本\nfim".encode())
    registry, _ = make_registry(workspace)
    assert edit(registry, "corpus/bom.txt", "fim", "FIM").ok
    assert target.read_bytes() == "﻿çafé 日本\nFIM".encode()


def test_neg_lf_files_and_write_file_are_unchanged(workspace) -> None:
    registry, _ = make_registry(workspace)
    lf = workspace.root / "corpus" / "lf.txt"
    lf.write_bytes(b"a\nb\n")
    assert edit(registry, "corpus/lf.txt", "b", "B").ok and lf.read_bytes() == b"a\nB\n"
    assert call(registry, "write_file", path="corpus/w.txt", content="a\r\nb\r\n").ok
    assert (workspace.root / "corpus" / "w.txt").read_bytes() == b"a\r\nb\r\n"


# --------------------------------------------------------------------------- LED-001
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_write_and_edit_report_content_hashes(workspace) -> None:
    registry, _ = make_registry(workspace)
    target = workspace.root / "corpus" / "h.md"
    created = call(registry, "write_file", path="corpus/h.md", content="hello")
    assert created.data["sha256_before"] is None
    assert created.data["sha256_after"] == sha(target)
    before = sha(target)
    edited = edit(registry, "corpus/h.md", "hello", "bye")
    assert edited.data["sha256_before"] == before
    assert edited.data["sha256_after"] == sha(target)


def test_an_unchanged_write_reports_equal_hashes(workspace) -> None:
    registry, _ = make_registry(workspace)
    call(registry, "write_file", path="corpus/same.md", content="x")
    again = call(registry, "write_file", path="corpus/same.md", content="x")
    assert again.data["sha256_before"] == again.data["sha256_after"]


def test_the_ledger_carries_the_file_hashes(workspace) -> None:
    from test_w2a import make_agent, tool_call

    from panda_core.provider import DoneEvent, TextDelta

    agent, session, _, _ = make_agent(
        workspace,
        [
            [tool_call("write_file", path="corpus/l.md", content="hello")],
            [TextDelta("ok"), DoneEvent(finish_reason="stop")],
        ],
    )
    asyncio.run(agent.run_turn("write"))
    results = [e for e in session.events() if e.type == "tool.result"]
    assert results[0].data["sha256_after"] == sha(workspace.root / "corpus" / "l.md")
    ends = [e for e in session.events() if e.type == "turn.end"]
    assert HEX64.fullmatch(ends[-1].data["tree_sha256"])


def test_tree_hash_is_stable_across_copies_and_changes_with_one_byte(
    workspace, tmp_path
) -> None:
    import shutil

    from panda_core.treehash import tree_hash

    first = tree_hash(workspace.root)
    copy = tmp_path / "copy"
    shutil.copytree(workspace.root, copy, ignore=shutil.ignore_patterns(".panda"))
    assert tree_hash(copy).digest == first.digest
    (copy / "corpus" / "evidence.md").write_bytes(b"# evidence!\n")
    assert tree_hash(copy).digest != first.digest


def test_tree_hash_ignores_the_ledger_and_other_non_corpus_files(workspace) -> None:
    from panda_core.treehash import tree_hash

    before = tree_hash(workspace.root).digest
    SessionLog.create(workspace.sessions_dir, label="noise")
    (workspace.root / "scratch.tmp").write_text("x")
    assert tree_hash(workspace.root).digest == before
