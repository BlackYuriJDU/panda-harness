"""Wave 2B: processes, providers, adapters, byte-exact edits and content hashes.

SEC-006/008, PROV-002/003/004, ADP-001/002, EDIT-001, LED-001. Synthetic data only;
providers are fixtures, never the network.
"""

from __future__ import annotations

import asyncio
import os
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
