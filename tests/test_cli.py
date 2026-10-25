"""CLI behavior: exit codes, JSON contracts, and the fail-closed surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from panda_cli.main import app
from panda_core.session import SessionLog

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


def test_version() -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.stdout.startswith("panda ")


def test_status_outside_workspace(tmp_path: Path) -> None:
    result = invoke("status", "--root", str(tmp_path), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "config_error"


def test_status_inside_workspace(workspace) -> None:
    result = invoke("status", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["workspace"]["root"] == str(workspace.root)
    assert payload["last_session"] is None


def test_study_list(workspace) -> None:
    result = invoke("study", "list", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [study["id"] for study in payload["studies"]] == ["gse160208", "caso-referencia"]


def test_study_list_track_filter(workspace) -> None:
    result = invoke(
        "study", "list", "--root", str(workspace.root), "--track", "simulated", "--json"
    )
    payload = json.loads(result.stdout)
    assert [study["id"] for study in payload["studies"]] == ["caso-referencia"]


def test_study_show(workspace) -> None:
    result = invoke("study", "show", "gse160208", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["study"]["frontmatter"]["study_id"] == "gse160208"
    assert "Body text" in payload["study"]["body"]


def test_study_show_missing(workspace) -> None:
    result = invoke("study", "show", "missing", "--root", str(workspace.root))
    assert result.exit_code == 2
    assert "study not found" in result.output


def test_policy_check_allow(workspace) -> None:
    result = invoke(
        "policy", "check", "write", "corpus/studies/x.md", "--root", str(workspace.root), "--json"
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["effect"] == "allow"
    assert payload["policy_source"] == "file"


def test_policy_check_deny(workspace) -> None:
    result = invoke(
        "policy", "check", "write", "README.md", "--root", str(workspace.root), "--json"
    )
    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    assert payload["effect"] == "deny"


def test_policy_check_traversal_denied(workspace) -> None:
    result = invoke(
        "policy",
        "check",
        "write",
        "corpus/../README.md",
        "--root",
        str(workspace.root),
        "--json",
    )
    assert result.exit_code == 5
    assert json.loads(result.stdout)["effect"] == "deny"


def test_policy_check_approve_once(workspace) -> None:
    result = invoke(
        "policy",
        "check",
        "shell",
        "cargo install x",
        "--root",
        str(workspace.root),
        "--approve",
        "--json",
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["effect"] == "allow"
    assert payload["approved"] is True


def test_policy_show(workspace) -> None:
    result = invoke("policy", "show", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source"] == "file"
    assert len(payload["policy"]["rules"]) > 10


def test_policy_explain(workspace) -> None:
    result = invoke(
        "policy",
        "explain",
        "shell",
        "git push origin main",
        "--root",
        str(workspace.root),
        "--json",
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["decision"]["effect"] == "ask"
    assert any(rule["id"] == "shell-push" for rule in payload["matching_rules"])


def test_init_dry_run(fake_corpus: Path) -> None:
    result = invoke("init", "--root", str(fake_corpus), "--dry-run", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert not (fake_corpus / ".panda").exists()
    assert not (fake_corpus / "opencode.json").exists()


def test_session_lifecycle(workspace) -> None:
    started = invoke("session", "start", "--root", str(workspace.root), "--json")
    assert started.exit_code == 0
    session_id = json.loads(started.stdout)["session_id"]

    listed = invoke("session", "list", "--root", str(workspace.root), "--json")
    assert listed.exit_code == 0
    assert json.loads(listed.stdout)["sessions"][0]["session_id"] == session_id

    verified = invoke("session", "verify", session_id, "--root", str(workspace.root), "--json")
    assert verified.exit_code == 0
    assert json.loads(verified.stdout)["ok"] is True

    closed = invoke("session", "close", session_id, "--root", str(workspace.root), "--json")
    assert closed.exit_code == 0
    assert json.loads(closed.stdout)["ok"] is True

    verified = invoke("session", "verify", session_id, "--root", str(workspace.root), "--json")
    assert verified.exit_code == 0


def test_session_show(workspace) -> None:
    started = invoke("session", "start", "--root", str(workspace.root), "--json")
    session_id = json.loads(started.stdout)["session_id"]
    shown = invoke("session", "show", session_id, "--root", str(workspace.root), "--json")
    assert shown.exit_code == 0
    payload = json.loads(shown.stdout)
    assert payload["verification"]["ok"] is True
    assert payload["events"][0]["type"] == "session.start"


def test_validate_success(workspace) -> None:
    result = invoke("validate", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["summary"]["passed"] == 10


def test_validate_failure(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANDA_FAKE_FAIL", "numbers")
    result = invoke("validate", "--root", str(workspace.root), "--json")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["summary"]["failed"] == 1


def test_validate_record_creates_closed_session(workspace) -> None:
    result = invoke("validate", "--root", str(workspace.root), "--record", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    session_id = payload["session_id"]
    log = SessionLog.resolve(workspace.sessions_dir, session_id)
    assert log.summary().closed is True
    assert log.verify().ok


def test_run_dry_run(wired_workspace) -> None:
    result = invoke(
        "run", "validate the corpus", "--root", str(wired_workspace.root), "--dry-run"
    )
    assert result.exit_code == 0
    assert "dry-run:" in result.output
    assert "--standalone" in result.output
    assert "session:" in result.output


def test_run_shared_dry_run(wired_workspace) -> None:
    result = invoke(
        "run",
        "validate the corpus",
        "--root",
        str(wired_workspace.root),
        "--dry-run",
        "--shared",
    )
    assert result.exit_code == 0
    assert "--standalone" not in result.output


def test_run_requires_task(wired_workspace) -> None:
    result = invoke("run", "--root", str(wired_workspace.root), "--dry-run")
    assert result.exit_code == 2
    assert "task is required" in result.output


def test_run_rejects_standalone_and_shared(wired_workspace) -> None:
    result = invoke(
        "run", "x", "--root", str(wired_workspace.root), "--standalone", "--shared"
    )
    assert result.exit_code == 2


def test_mcp_tools_lists_contract() -> None:
    result = invoke("mcp", "tools")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "panda_validate" in payload["opencode_tools"]
    assert payload["resources"] == ["panda://workspace", "panda://studies"]
