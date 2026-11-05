"""Wave 7b: `panda policy trust` — trusting a workspace policy is an explicit act."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from panda_cli.main import app

RUNNER = CliRunner()


def _policy(workspace, rules) -> None:
    workspace.policy_path.write_text(
        json.dumps({"schema_version": 1, "default": "ask", "rules": rules}), encoding="utf-8"
    )


WILD = {"id": "corpus-shell", "actions": ["shell"], "resources": ["*"], "effect": "allow"}
NARROW = {"id": "lab-pytest", "actions": ["shell"], "resources": ["pytest *"], "effect": "allow"}


def _run(workspace, *args, input=None):
    command = ["policy", "trust", "--root", str(workspace.root), *args]
    return RUNNER.invoke(app, command, input=input)


def test_show_lists_what_trust_would_allow_and_changes_nothing(workspace) -> None:
    from panda_core.trust import is_trusted

    _policy(workspace, [WILD, NARROW])
    result = _run(workspace, "--show", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["trusted"] is False
    assert {r["id"]: r["wildcard"] for r in payload["loosening_rules"]} == {
        "corpus-shell": True, "lab-pytest": False,
    }
    assert not is_trusted(workspace.root, workspace.policy_path)


def test_trust_without_a_terminal_needs_yes(workspace) -> None:
    from panda_core.trust import is_trusted

    _policy(workspace, [NARROW])
    result = _run(workspace)
    assert result.exit_code == 2
    assert not is_trusted(workspace.root, workspace.policy_path)


def test_yes_grants_and_editing_the_file_withdraws(workspace) -> None:
    from panda_core.trust import is_trusted

    _policy(workspace, [NARROW])
    assert _run(workspace, "--yes").exit_code == 0
    assert is_trusted(workspace.root, workspace.policy_path)
    _policy(workspace, [NARROW, WILD])
    assert not is_trusted(workspace.root, workspace.policy_path)


def test_revoke_withdraws_trust(workspace) -> None:
    from panda_core.trust import is_trusted

    _policy(workspace, [NARROW])
    _run(workspace, "--yes")
    assert _run(workspace, "--revoke").exit_code == 0
    assert not is_trusted(workspace.root, workspace.policy_path)


def test_the_default_policy_file_written_by_init_needs_no_trust(workspace) -> None:
    from panda_core.policy import load_policy
    from panda_core.trust import loosening_rules

    assert loosening_rules(load_policy(workspace.policy_path)) == []


def test_a_legacy_a5_policy_file_needs_no_trust(workspace) -> None:
    from panda_core.policy import load_policy
    from panda_core.trust import loosening_rules

    _policy(workspace, [{"id": "shell-readonly", "actions": ["shell"], "resources": [
        "git status*", "git diff*", "git log*", "git show*", "git branch*", "panda validate*",
    ], "effect": "allow"}])
    assert loosening_rules(load_policy(workspace.policy_path)) == []


def test_trust_is_stored_outside_the_workspace(workspace) -> None:
    from panda_core.trust import trust_path

    _policy(workspace, [NARROW])
    _run(workspace, "--yes")
    assert workspace.root not in trust_path().parents


def test_the_operator_is_told_when_allow_rules_are_ignored(workspace) -> None:
    from panda_cli.runtime import ChatRuntime, policy_notice

    _policy(workspace, [NARROW, WILD])
    runtime = ChatRuntime(workspace, label="w7b")
    try:
        state = runtime.policy_state
        assert state == {"source": "file", "trusted": False, "ignored_allow_rules": 2}
        assert "2 allow rules ignored" in policy_notice(state)
        start = next(e for e in runtime.session.events() if e.type == "session.start")
        assert start.data["metadata"]["workspace_policy"]["trusted"] is False
    finally:
        runtime.close()


def test_no_notice_for_the_default_policy(workspace) -> None:
    from panda_cli.runtime import ChatRuntime, policy_notice

    runtime = ChatRuntime(workspace, label="w7b")
    try:
        assert policy_notice(runtime.policy_state) is None
    finally:
        runtime.close()


# --------------------------------------------------------------------------- W8 provenance
def _decisions(runtime):
    return [e.data for e in runtime.session.events() if e.type == "approval.decision"]


def test_the_ledger_says_who_approved(workspace) -> None:
    import asyncio

    from panda_cli.runtime import ChatRuntime

    async def person(request) -> bool:
        return True

    for kwargs, expected in (({"auto": True}, "auto"), ({"approver": person}, "human"),
                             ({}, "policy")):
        runtime = ChatRuntime(workspace, label="w8", **kwargs)
        try:
            asyncio.run(runtime.tools.call("run_shell", {"argv": ["pytest", "--version"]}))
            decision = _decisions(runtime)[-1]
            assert decision["decided_by"] == expected
            assert decision["effect_class"] == "exec_local"
            start = next(e for e in runtime.session.events() if e.type == "session.start")
            assert start.data["metadata"]["approval_mode"] in {"auto", "human", "none"}
        finally:
            runtime.close()


def test_the_operator_shell_route_is_recorded_in_the_ledger(workspace) -> None:
    import asyncio

    from panda_cli.runtime import ChatRuntime

    runtime = ChatRuntime(workspace, label="w8")
    try:
        asyncio.run(runtime.agent.operator_tool("run_shell", {"argv": ["git", "status"]}))
        events = [e for e in runtime.session.events() if e.type in {"tool.call", "tool.result"}]
        assert [e.type for e in events] == ["tool.call", "tool.result"]
        assert all(e.data["origin"] == "operator" for e in events)
    finally:
        runtime.close()


def test_the_operator_shell_route_stops_when_the_ledger_cannot_record(
    workspace, monkeypatch
) -> None:
    import asyncio

    import pytest

    from panda_cli.runtime import ChatRuntime
    from panda_core.errors import SessionIntegrityError

    runtime = ChatRuntime(workspace, label="w8")
    ran: list[object] = []
    monkeypatch.setattr(runtime.tools, "call", lambda *a, **k: ran.append(a))

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runtime.session, "append", broken)
    with pytest.raises(SessionIntegrityError):
        asyncio.run(runtime.agent.operator_tool("run_shell", {"argv": ["git", "status"]}))
    assert ran == []


def test_session_end_records_a_cancelled_last_turn(workspace, monkeypatch) -> None:
    import asyncio

    import pytest

    from panda_cli.runtime import ChatRuntime

    runtime = ChatRuntime(workspace, label="w8")

    async def cancelled(message):
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime.agent, "run_turn", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.ask("x"))
    runtime.close()
    end = [e for e in runtime.session.events() if e.type == "session.end"][-1]
    assert end.data["status"] == "cancelled"


def test_a_failed_session_end_is_reported_not_swallowed(workspace, monkeypatch) -> None:
    from panda_cli.runtime import ChatRuntime

    runtime = ChatRuntime(workspace, label="w8")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runtime.session, "close", broken)
    runtime.close()
    assert runtime.close_error and "disk full" in runtime.close_error
