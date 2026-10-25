"""Session ledger: hash chain, anchor, redaction, truncation, and rotation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panda_core.errors import ConfigError, SessionIntegrityError
from panda_core.session import SessionLog, event_hash, list_sessions, new_session_id


@pytest.fixture()
def sessions_dir(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_create_appends_start_and_verifies(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="unit", metadata={"suite": "session"})
    verification = log.verify()
    assert verification.ok
    assert verification.count == 1
    assert verification.anchor_ok is True
    assert verification.errors == []
    summary = log.summary()
    assert summary.label == "unit"
    assert summary.closed is False
    assert summary.events == 1


def test_hash_chain_links_events(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="chain")
    first = log.append("note", {"message": "one"})
    second = log.append("note", {"message": "two"})
    records = read_lines(log.path)
    assert records[0]["prev"] is None
    assert records[1]["prev"] == records[0]["hash"]
    assert records[2]["prev"] == records[1]["hash"]
    assert first.prev == records[0]["hash"]
    assert second.prev == first.hash
    assert [record["seq"] for record in records] == [1, 2, 3]
    assert records[2]["session_id"] == log.session_id


def test_verify_detects_tampering(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="tamper")
    log.append("note", {"message": "original"})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["data"]["message"] = "rewritten"
    lines[1] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    verification = log.verify()
    assert not verification.ok
    codes = {issue.code for issue in verification.errors}
    assert "hash" in codes
    # A middle-record edit leaves the tail intact, so the chain catches it.
    assert verification.anchor_ok is True


def test_anchor_detects_coordinated_tail_rewrite(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="tail-rewrite")
    log.append("note", {"message": "original"})
    records = read_lines(log.path)
    records[-1]["data"]["message"] = "rewritten"
    records[-1]["hash"] = event_hash(
        {key: value for key, value in records[-1].items() if key != "hash"}
    )
    log.path.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    verification = log.verify()
    assert not verification.ok
    assert any(issue.code == "anchor_mismatch" for issue in verification.errors)


def test_verify_detects_truncation(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="truncate")
    log.append("note", {"message": "one"})
    log.append("note", {"message": "two"})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8", newline="\n")

    verification = log.verify()
    assert not verification.ok
    codes = {issue.code for issue in verification.errors}
    assert "anchor_mismatch" in codes


def test_append_refuses_truncated_log(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="truncate-append")
    log.append("note", {"message": "one"})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8", newline="\n")

    reopened = SessionLog.open(log.path)
    with pytest.raises(SessionIntegrityError):
        reopened.append("note", {"message": "should not append"})


def test_redaction_is_applied_before_hashing(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="redact")
    log.append(
        "run.start",
        {
            "api_key": "sk-or-v1-abcdefghijklmnopqrstuvwxyz",
            "authorization": "Bearer abcdefghijklmnop",
            "input_tokens": 42,
            "note": "token=supersecretvalue",
        },
    )
    record = read_lines(log.path)[1]
    assert record["data"]["api_key"] == "[redacted]"
    assert record["data"]["authorization"] == "[redacted]"
    assert record["data"]["input_tokens"] == 42
    assert record["data"]["note"] == "token=[redacted]"
    assert log.verify().ok


def test_redaction_can_be_disabled(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="raw", redact_enabled=False)
    log.append("note", {"api_key": "sk-or-v1-abcdefghijklmnopqrstuvwxyz"})
    record = read_lines(log.path)[1]
    assert record["data"]["api_key"].startswith("sk-or-v1-")


def test_close_is_terminal(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="close")
    log.close("completed", {"result": "ok"})
    assert log.is_closed()
    assert log.summary().closed
    with pytest.raises(ConfigError):
        log.close("again")


def test_invalid_session_id_rejected(sessions_dir: Path) -> None:
    with pytest.raises(ConfigError):
        SessionLog.create(sessions_dir, session_id="not a session")


def test_new_session_id_is_unique_and_valid() -> None:
    ids = {new_session_id() for _ in range(20)}
    assert len(ids) == 20
    assert all(item.startswith("s_") for item in ids)


def test_resolve_by_id_and_latest(sessions_dir: Path) -> None:
    first = SessionLog.create(sessions_dir, label="first")
    second = SessionLog.create(sessions_dir, label="second")
    assert SessionLog.resolve(sessions_dir, first.session_id).session_id == first.session_id
    assert SessionLog.latest(sessions_dir).session_id == second.session_id
    assert SessionLog.resolve(sessions_dir, "latest").session_id == second.session_id


def test_list_sessions_counts_and_skips_corrupt(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="listed")
    log.close("ok")
    (sessions_dir / "s_20260101T000000Z_badbad.jsonl").write_text(
        "not json\n", encoding="utf-8"
    )
    summaries = list_sessions(sessions_dir)
    assert len(summaries) == 1
    assert summaries[0].closed is True
    assert summaries[0].events == 2


def test_verify_reports_blank_line(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="blank")
    with open(log.path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n")
    verification = log.verify()
    assert not verification.ok
    assert any(issue.code == "blank_line" for issue in verification.errors)


def test_events_after_end_are_reported(sessions_dir: Path) -> None:
    log = SessionLog.create(sessions_dir, label="after-end")
    log.close("ok")
    # Bypass the public append guard to simulate a forged trailing event.
    record = read_lines(log.path)[-1].copy()
    record["seq"] = record["seq"] + 1
    record["type"] = "note"
    record["data"] = {"message": "after end"}
    record["prev"] = read_lines(log.path)[-1]["hash"]
    record["hash"] = event_hash({key: value for key, value in record.items() if key != "hash"})
    with open(log.path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    verification = log.verify()
    assert not verification.ok
    assert any(issue.code == "event_after_end" for issue in verification.errors)
