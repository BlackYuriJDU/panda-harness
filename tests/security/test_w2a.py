"""Wave 2A: a ledger that fails closed, and an agent loop that cannot lose its task.

PROV-001, SES-001/002/003, PERF-001, AGT-001/002/003/004. Synthetic data only.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import pytest

from panda_core.errors import SessionIntegrityError
from panda_core.session import SessionLog, event_hash


def forge(session: SessionLog, data: dict) -> None:
    """Append an event with correct seq/prev/hash, as a local attacker could."""

    last = json.loads(session.path.read_text(encoding="utf-8").splitlines()[-1])
    record = {
        "schema": last["schema"],
        "seq": last["seq"] + 1,
        "ts": last["ts"],
        "session_id": last["session_id"],
        "type": "note",
        "data": data,
        "prev": last["hash"],
    }
    record["hash"] = event_hash(record)
    with open(session.path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def types(session: SessionLog) -> list[str]:
    return [json.loads(line)["type"] for line in session.path.read_text("utf-8").splitlines()]


# --------------------------------------------------------------------------- SES-003
def test_forged_event_is_not_laundered_by_the_next_append(tmp_path: Path) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    session.append("note", {"i": 1})
    forge(session, {"FORGED": True})
    assert not SessionLog.open(session.path).verify().ok
    with pytest.raises(SessionIntegrityError):
        SessionLog.open(session.path).append("note", {"legit": 1})
    assert not SessionLog.open(session.path).verify().ok


def test_a_stale_anchor_behind_by_many_events_also_blocks_append(tmp_path: Path) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    session.append("note", {"i": 1})
    forge(session, {"A": 1})
    forge(session, {"B": 2})
    with pytest.raises(SessionIntegrityError):
        SessionLog.open(session.path).append("note", {"legit": 1})


# --------------------------------------------------------------------------- SES-002
def test_crash_between_fsync_and_anchor_needs_an_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    session.append("note", {"i": 1})
    real = SessionLog._write_anchor

    def boom(self: SessionLog, seq: int, digest: str) -> None:
        raise OSError("simulated crash after fsync")

    monkeypatch.setattr(SessionLog, "_write_anchor", boom)
    with pytest.raises(OSError):
        session.append("note", {"i": 2})
    monkeypatch.setattr(SessionLog, "_write_anchor", real)

    reopened = SessionLog.open(session.path)
    assert not reopened.verify().ok
    with pytest.raises(SessionIntegrityError):  # no silent cure
        reopened.append("note", {"i": 3})

    reopened.recover()
    assert "session.recovered" in types(reopened)
    assert SessionLog.open(session.path).verify().ok
    reopened.append("note", {"i": 3})
    assert SessionLog.open(session.path).verify().ok


def test_recovery_discloses_the_adopted_event(tmp_path: Path) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    forge(session, {"FORGED": True})
    reopened = SessionLog.open(session.path)
    reopened.recover()
    recovered = [
        json.loads(line)
        for line in session.path.read_text("utf-8").splitlines()
        if json.loads(line)["type"] == "session.recovered"
    ][0]
    assert recovered["data"]["adopted_seq"] == 2
    assert recovered["data"]["adopted_type"] == "note"
    assert "FORGED" in session.path.read_text("utf-8")  # nothing is erased


def test_recover_is_a_no_op_on_a_healthy_session(tmp_path: Path) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    before = session.path.read_text("utf-8")
    assert session.recover() is False
    assert session.path.read_text("utf-8") == before


def test_recover_refuses_when_the_log_is_shorter_than_the_anchor(tmp_path: Path) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    for i in range(3):
        session.append("note", {"i": i})
    lines = session.path.read_text("utf-8").splitlines()
    session.path.write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")
    with pytest.raises(SessionIntegrityError):
        SessionLog.open(session.path).recover()


def test_recover_refuses_a_rewritten_history(tmp_path: Path) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    for i in range(3):
        session.append("note", {"i": i})
    lines = [json.loads(line) for line in session.path.read_text("utf-8").splitlines()]
    lines[1]["data"]["i"] = 999  # hash no longer matches
    session.path.write_text(
        "\n".join(json.dumps(r, separators=(",", ":")) for r in lines) + "\n", encoding="utf-8"
    )
    with pytest.raises(SessionIntegrityError):
        SessionLog.open(session.path).recover()


def test_partial_os_write_is_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.write

    def short_write(fd: int, data: bytes) -> int:
        return real(fd, bytes(data[:7]))

    session = SessionLog.create(tmp_path / "s", label="t")
    monkeypatch.setattr("panda_core.session.os.write", short_write)
    session.append("note", {"payload": "x" * 500})
    monkeypatch.undo()
    assert SessionLog.open(session.path).verify().ok


# --------------------------------------------------------------------------- SES-001
@pytest.mark.parametrize("size", [200_000, 3_000_000])
def test_a_last_event_larger_than_the_tail_window_can_be_reopened(
    tmp_path: Path, size: int
) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    session.append("note", {"m": "x" * size})
    reopened = SessionLog.open(session.path)
    reopened.append("note", {"m": "after"})
    assert SessionLog.open(session.path).verify().ok


def test_run_plan_with_a_huge_task_stays_small(workspace) -> None:
    from typer.testing import CliRunner

    from panda_cli.main import app

    result = CliRunner().invoke(
        app, ["run", "--dry-run", "--root", str(workspace.root), "T" * 300_000]
    )
    assert result.exit_code == 0, result.output
    log = sorted((workspace.root / ".panda" / "sessions").glob("s_*.jsonl"))[-1]
    assert max(len(line.encode()) for line in log.read_text("utf-8").splitlines()) < 128 * 1024


# --------------------------------------------------------------------------- PERF-001
def test_is_closed_does_not_reread_the_ledger(tmp_path: Path) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    for i in range(600):
        session.append("note", {"i": i})
    assert session.is_closed() is False
    started = time.perf_counter()
    for _ in range(300):
        session.is_closed()
    per_call_ms = (time.perf_counter() - started) / 300 * 1000
    assert per_call_ms < 0.1, per_call_ms


def test_is_closed_follows_another_instance(tmp_path: Path) -> None:
    session = SessionLog.create(tmp_path / "s", label="t")
    other = SessionLog.open(session.path)
    assert session.is_closed() is False
    other.close("completed")
    assert session.is_closed() is True


# ------------------------------------------------------------ `panda session recover`
def _crashed_session(workspace, monkeypatch: pytest.MonkeyPatch) -> SessionLog:
    session = SessionLog.create(workspace.sessions_dir, label="crash")
    real = SessionLog._write_anchor

    def boom(self: SessionLog, seq: int, digest: str) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(SessionLog, "_write_anchor", boom)
    with pytest.raises(OSError):
        session.append("note", {"i": 1})
    monkeypatch.setattr(SessionLog, "_write_anchor", real)
    return session


def test_cli_recover_repairs_a_crashed_session(
    workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from panda_cli.main import app

    session = _crashed_session(workspace, monkeypatch)
    runner = CliRunner()
    base = ["--root", str(workspace.root)]
    assert runner.invoke(app, ["session", "verify", session.session_id, *base]).exit_code == 1
    result = runner.invoke(app, ["session", "recover", session.session_id, *base, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["recovered"] is True
    assert runner.invoke(app, ["session", "verify", session.session_id, *base]).exit_code == 0


def test_cli_recover_on_a_healthy_session_does_nothing(workspace) -> None:
    from typer.testing import CliRunner

    from panda_cli.main import app

    session = SessionLog.create(workspace.sessions_dir, label="ok")
    result = CliRunner().invoke(
        app, ["session", "recover", session.session_id, "--root", str(workspace.root), "--json"]
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["recovered"] is False


def test_cli_recover_refuses_a_forged_chain_with_exit_4(workspace) -> None:
    from typer.testing import CliRunner

    from panda_cli.main import app

    session = SessionLog.create(workspace.sessions_dir, label="bad")
    for i in range(3):
        session.append("note", {"i": i})
    forge(session, {"a": 1})
    forge(session, {"b": 2})
    result = CliRunner().invoke(
        app, ["session", "recover", session.session_id, "--root", str(workspace.root)]
    )
    assert result.exit_code == 4


# --------------------------------------------------------------------------- agent helpers
import asyncio  # noqa: E402
from typing import Any  # noqa: E402

from panda_core.agent import Agent  # noqa: E402
from panda_core.policy import PolicyEngine, load_policy_or_default  # noqa: E402
from panda_core.provider import DoneEvent, TextDelta, ToolCall  # noqa: E402
from panda_core.tools import ToolContext, ToolRegistry  # noqa: E402


class Script:
    def __init__(self, script: list[list[Any]]) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(self, messages, tools=None, model=None, session_id=None):
        self.calls.append([dict(m) for m in messages])
        events = self.script.pop(0) if self.script else [DoneEvent(finish_reason="stop")]
        for event in events:
            if isinstance(event, BaseException):
                raise event
            yield event


def make_agent(workspace, script, *, approver=None, max_steps=4, context_window=None):
    session = SessionLog.create(workspace.sessions_dir, label="w2a")
    policy, _ = load_policy_or_default(workspace.policy_path)
    registry = ToolRegistry(
        ToolContext(
            workspace=workspace,
            policy=PolicyEngine(policy, root=workspace.root),
            session=session,
        )
    )
    seen: list[Any] = []
    agent = Agent(
        workspace,
        Script(script),
        registry,
        session=session,
        max_steps=max_steps,
        approver=approver,
        context_window=context_window,
        on_event=seen.append,
    )
    return agent, session, registry, seen


def tool_call(name: str, **arguments: Any) -> DoneEvent:
    call = ToolCall(id="c1", name=name, arguments=json.dumps(arguments))
    return DoneEvent(finish_reason="tool_calls", tool_calls=(call,))


async def approve_all(request) -> bool:
    return True


# --------------------------------------------------------------------------- PROV-001
def test_a_broken_ledger_stops_the_turn_before_any_write(workspace, monkeypatch) -> None:
    agent, session, _, seen = make_agent(
        workspace,
        [[tool_call("write_file", path="corpus/h.md", content="hello")]],
    )
    real = SessionLog.append

    def fail_on_tool_call(self, event_type, data=None):
        if event_type == "tool.call":
            raise SessionIntegrityError("simulated anchor failure")
        return real(self, event_type, data)

    monkeypatch.setattr(SessionLog, "append", fail_on_tool_call)
    with pytest.raises(SessionIntegrityError):
        asyncio.run(agent.run_turn("write it"))
    assert not (workspace.root / "corpus" / "h.md").exists()
    assert any(e.kind == "turn_end" and e.data.get("status") == "ledger_failed" for e in seen)


def test_a_broken_ledger_starts_no_subprocess(workspace, tmp_path, monkeypatch) -> None:
    marker = tmp_path / "ran.txt"
    agent, session, _, _ = make_agent(
        workspace,
        [[tool_call("run_shell", argv=["touch", str(marker)])]],
        approver=approve_all,
    )
    session._write_anchor(999, "0" * 64)  # the audit's own scenario: anchor ahead of the log
    with pytest.raises(SessionIntegrityError):
        asyncio.run(agent.run_turn("touch it"))
    assert not marker.exists()


def test_a_full_disk_is_a_ledger_failure_not_a_silent_skip(workspace, monkeypatch) -> None:
    import errno

    agent, session, _, _ = make_agent(
        workspace, [[tool_call("write_file", path="corpus/d.md", content="x")]]
    )

    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("panda_core.session.os.write", full)
    with pytest.raises(SessionIntegrityError):
        asyncio.run(agent.run_turn("write"))
    monkeypatch.undo()
    assert not (workspace.root / "corpus" / "d.md").exists()


def test_a_failing_approval_record_denies_the_action(workspace, monkeypatch) -> None:
    agent, session, _, _ = make_agent(
        workspace,
        [[tool_call("write_file", path="pipeline/a.py", content="x")], []],
        approver=approve_all,
    )
    real = SessionLog.append

    def fail_on_approval(self, event_type, data=None):
        if event_type.startswith("approval."):
            raise SessionIntegrityError("simulated")
        return real(self, event_type, data)

    monkeypatch.setattr(SessionLog, "append", fail_on_approval)
    with contextlib.suppress(SessionIntegrityError):
        asyncio.run(agent.run_turn("write"))
    assert not (workspace.root / "pipeline" / "a.py").exists()


def test_mcp_record_failure_is_raised(workspace, monkeypatch) -> None:
    from panda_core.service import PandaService

    session = SessionLog.create(workspace.sessions_dir, label="mcp")
    service = PandaService(workspace, session=session)

    def boom(self, event_type, data=None):
        raise SessionIntegrityError("simulated")

    monkeypatch.setattr(SessionLog, "append", boom)
    with pytest.raises(SessionIntegrityError):
        service.note("a note")


def test_neg_a_healthy_ledger_runs_the_tool(workspace) -> None:
    agent, session, _, _ = make_agent(
        workspace,
        [[tool_call("write_file", path="corpus/ok.md", content="fine")], [TextDelta("done")]],
    )
    asyncio.run(agent.run_turn("write"))
    assert (workspace.root / "corpus" / "ok.md").read_text() == "fine"
    assert session.verify().ok


# --------------------------------------------------------------------------- AGT-001
def test_an_unexpected_tool_exception_becomes_a_failed_result(workspace) -> None:
    agent, session, registry, _ = make_agent(
        workspace, [[tool_call("workspace_info")], [TextDelta("recovered")]]
    )

    async def broken(args):
        raise RuntimeError("handler exploded")

    registry._handlers["workspace_info"] = broken
    result = asyncio.run(agent.run_turn("go"))
    assert result.status == "completed"
    tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1 and "handler exploded" in tool_messages[0]["content"]
    assert "turn.end" in [e.type for e in session.events()]


def test_a_provider_crash_still_closes_the_turn(workspace) -> None:
    agent, session, _, _ = make_agent(workspace, [[RuntimeError("provider blew up")]])
    with pytest.raises(RuntimeError):
        asyncio.run(agent.run_turn("go"))
    ends = [e for e in session.events() if e.type == "turn.end"]
    assert ends and ends[-1].data["status"] == "error"


def test_a_cancelled_tool_leaves_no_unanswered_tool_calls(workspace) -> None:
    agent, session, registry, _ = make_agent(workspace, [[tool_call("workspace_info")]])

    async def hang(args):
        raise asyncio.CancelledError

    registry._handlers["workspace_info"] = hang
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent.run_turn("go"))
    calls = [m for m in agent.messages if m.get("tool_calls")]
    answered = {m["tool_call_id"] for m in agent.messages if m.get("role") == "tool"}
    assert {c["id"] for m in calls for c in m["tool_calls"]} <= answered


# --------------------------------------------------------------------------- AGT-003
@pytest.mark.parametrize("reason", ["length", "content_filter", "incomplete"])
def test_a_cut_off_answer_is_not_reported_as_completed(workspace, reason: str) -> None:
    agent, session, _, _ = make_agent(
        workspace, [[TextDelta("partial"), DoneEvent(finish_reason=reason)]]
    )
    result = asyncio.run(agent.run_turn("go"))
    assert result.status == "incomplete"
    ends = [e for e in session.events() if e.type == "turn.end"]
    assert ends[-1].data["status"] == "incomplete"
    assert ends[-1].data["finish_reason"] == reason


def test_truncated_tool_calls_are_not_executed(workspace) -> None:
    call = ToolCall(id="c1", name="write_file", arguments='{"path":"corpus/t.md","content":"x')
    agent, _, _, _ = make_agent(
        workspace, [[DoneEvent(finish_reason="length", tool_calls=(call,))]]
    )
    result = asyncio.run(agent.run_turn("go"))
    assert result.status == "incomplete"
    assert not (workspace.root / "corpus" / "t.md").exists()
    assert not any(m.get("tool_calls") for m in agent.messages)


@pytest.mark.parametrize("reason", ["stop", "tool_calls", None, "end_turn"])
def test_neg_normal_finish_reasons_complete(workspace, reason) -> None:
    agent, _, _, _ = make_agent(workspace, [[TextDelta("ok"), DoneEvent(finish_reason=reason)]])
    assert asyncio.run(agent.run_turn("go")).status == "completed"


# --------------------------------------------------------------------------- AGT-002
TASK = "PINNED_TASK_find_the_three_enriched_pathways"


def _bulk(agent: Agent, rounds: int) -> None:
    """Old conversation: user/assistant pairs and tool exchanges, each ~1 KB."""

    for index in range(rounds):
        agent.messages.append({"role": "user", "content": f"old question {index} " + "q" * 1_000})
        call_id = f"old{index}"
        agent.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        agent.messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": "r" * 1_000}
        )
        agent.messages.append({"role": "assistant", "content": f"answer {index} " + "a" * 1_000})


def test_the_task_survives_trimming_and_the_trim_is_recorded(workspace) -> None:
    agent, session, _, _ = make_agent(
        workspace, [[TextDelta("ok"), DoneEvent(finish_reason="stop")]], context_window=1_000
    )
    _bulk(agent, 12)
    asyncio.run(agent.run_turn(TASK))
    provider = agent.provider
    sent = provider.calls[0]
    assert any(m.get("role") == "user" and m.get("content") == TASK for m in sent)
    assert sent[0]["role"] == "system"
    trims = [e for e in session.events() if e.type == "context.trim"]
    assert trims, "the ledger must say the context was cut"
    data = trims[0].data
    assert data["removed"] > 0 and len(data["removed_sha256"]) == data["removed"]
    assert all(len(h) == 64 for h in data["removed_sha256"])


def test_a_visible_marker_replaces_the_removed_messages(workspace) -> None:
    agent, _, _, _ = make_agent(
        workspace, [[TextDelta("ok"), DoneEvent(finish_reason="stop")]], context_window=1_000
    )
    _bulk(agent, 12)
    asyncio.run(agent.run_turn(TASK))
    markers = [m for m in agent.messages if "[contexto resumido:" in str(m.get("content"))]
    assert len(markers) == 1
    assert "read_file" in markers[0]["content"]


def test_trimming_never_leaves_an_orphan_tool_message(workspace) -> None:
    agent, _, _, _ = make_agent(
        workspace, [[TextDelta("ok"), DoneEvent(finish_reason="stop")]], context_window=1_000
    )
    _bulk(agent, 12)
    asyncio.run(agent.run_turn(TASK))
    announced: set[str] = set()
    for message in agent.messages:
        for call in message.get("tool_calls") or []:
            announced.add(call["id"])
        if message.get("role") == "tool":
            assert message["tool_call_id"] in announced


def test_the_most_recent_messages_are_kept_whole(workspace) -> None:
    agent, _, _, _ = make_agent(
        workspace, [[TextDelta("ok"), DoneEvent(finish_reason="stop")]], context_window=1_000
    )
    _bulk(agent, 12)
    last_four = [dict(m) for m in agent.messages[-4:]]
    asyncio.run(agent.run_turn(TASK))
    kept = agent.messages
    for message in last_four:
        assert message in kept


def test_no_trim_event_when_everything_fits(workspace) -> None:
    script = [[TextDelta("ok"), DoneEvent(finish_reason="stop")]]
    agent, session, _, _ = make_agent(workspace, script)
    asyncio.run(agent.run_turn("short task"))
    assert not [e for e in session.events() if e.type == "context.trim"]


def test_a_second_trim_updates_one_marker_not_two(workspace) -> None:
    agent, _, _, _ = make_agent(
        workspace,
        [
            [TextDelta("ok"), DoneEvent(finish_reason="stop")],
            [TextDelta("ok2"), DoneEvent(finish_reason="stop")],
        ],
        context_window=1_000,
    )
    _bulk(agent, 8)
    asyncio.run(agent.run_turn(TASK))
    _bulk(agent, 8)
    asyncio.run(agent.run_turn(TASK + "_second"))
    markers = [m for m in agent.messages if "[contexto resumido:" in str(m.get("content"))]
    assert len(markers) == 1
