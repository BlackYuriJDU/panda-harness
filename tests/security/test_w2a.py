"""Wave 2A: a ledger that fails closed, and an agent loop that cannot lose its task.

PROV-001, SES-001/002/003, PERF-001, AGT-001/002/003/004. Synthetic data only.
"""

from __future__ import annotations

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
