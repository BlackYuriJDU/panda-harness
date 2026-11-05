"""Wave 12: the agent-tool interface and the machine contract of `panda run`."""

from __future__ import annotations

import json

import pytest


# --------------------------------------------------------------------------- R2-404
@pytest.mark.parametrize(
    ("status", "blocked", "code"),
    [("completed", 0, 0), ("completed", 2, 5), ("max_steps", 0, 7), ("incomplete", 0, 7),
     ("budget_exceeded", 0, 7), ("cancelled", 0, 130)],
)
def test_run_status_maps_to_the_documented_exit_code(status, blocked, code) -> None:
    from panda_cli.main import run_exit_code

    assert run_exit_code(status, blocked) == code


def test_run_json_emits_error_object_on_provider_error(workspace, monkeypatch) -> None:
    from typer.testing import CliRunner

    from panda_cli.main import app

    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["run", "--root", str(workspace.root), "--json", "hello"])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and payload["exit_code"] == result.exit_code
    assert payload["error"]["code"] and payload["error"]["message"]


# --------------------------------------------------------------------------- R2-401
def _registry(workspace):
    from panda_core.policy import PolicyEngine, default_policy
    from panda_core.tools import ToolContext, ToolRegistry

    async def yes(request) -> bool:
        return True

    engine = PolicyEngine(default_policy(), root=workspace.root)
    return ToolRegistry(ToolContext(workspace=workspace, policy=engine, approve=yes))


def _call(registry, name, args):
    import asyncio

    return asyncio.run(registry.call(name, args))


def test_string_false_is_rejected_for_boolean_flags(workspace) -> None:
    path = workspace.root / "corpus" / "x.md"
    path.write_text("a a a\n", encoding="utf-8")
    result = _call(_registry(workspace), "edit_file", {
        "path": "corpus/x.md", "old_string": "a", "new_string": "b", "replace_all": "false"})
    assert not result.ok and "replace_all" in result.content
    assert path.read_text(encoding="utf-8") == "a a a\n"


def test_unknown_arguments_are_refused_not_ignored(workspace) -> None:
    result = _call(_registry(workspace), "read_file", {"path": "README.md", "offset": 10})
    assert not result.ok and "unknown argument" in result.content and "offset" in result.content


def test_missing_required_argument_names_the_field(workspace) -> None:
    result = _call(_registry(workspace), "read_file", {})
    assert not result.ok and "path" in result.content


def test_every_tool_schema_is_a_valid_json_schema(workspace) -> None:
    from jsonschema import Draft202012Validator

    for spec in _registry(workspace).specs():
        Draft202012Validator.check_schema(spec.parameters)


# --------------------------------------------------------------------------- R2-408 / R2-413
def _limited(workspace, limit: int):
    registry = _registry(workspace)
    registry.ctx.result_limit = limit
    return registry


def test_read_file_stops_at_the_result_budget_and_says_where_to_resume(workspace) -> None:
    path = workspace.root / "corpus" / "big.md"
    path.write_text("".join(f"line {n:04d} " + "x" * 90 + "\n" for n in range(1, 2001)),
                    encoding="utf-8")
    result = _call(_limited(workspace, 20_000), "read_file",
                   {"path": "corpus/big.md", "max_lines": 2000})
    assert result.ok and len(result.content) <= 20_000
    end = result.data["end_line"]
    assert 100 < end < 2000 and result.data["truncated"] is True
    assert result.data["next_start_line"] == end + 1
    assert f"call read_file with start_line={end + 1}" in result.content
    resumed = _call(_limited(workspace, 20_000), "read_file",
                    {"path": "corpus/big.md", "start_line": end + 1, "max_lines": 5})
    assert f"line {end + 1:04d}" in resumed.content


def test_read_file_cuts_a_single_huge_line(workspace) -> None:
    (workspace.root / "corpus" / "one.md").write_text("y" * 500_000, encoding="utf-8")
    result = _call(_registry(workspace), "read_file", {"path": "corpus/one.md"})
    assert result.ok and len(result.content) < 3_000
    assert result.data["lines_cut"] == 1 and "more chars]" in result.content


def test_the_default_budget_follows_the_context_window(workspace) -> None:
    registry = _registry(workspace)
    window = workspace.config.provider.context_window
    assert registry.ctx.max_result_chars() == max(8_000, min(200_000, int(window * 0.8)))


def test_every_tool_result_is_bounded(workspace) -> None:
    (workspace.root / "corpus" / "wide.md").write_text(
        "\n".join("NEEDLE " + "z" * 1500 for _ in range(400)), encoding="utf-8")
    result = _call(_limited(workspace, 10_000), "grep",
                   {"pattern": "NEEDLE", "max_matches": 400})
    assert len(result.content) <= 10_000 + 200
    assert result.data["result_truncated"] is True
    assert "narrow the request" in result.content


def test_read_file_past_the_end_is_an_error(workspace) -> None:
    (workspace.root / "corpus" / "short.md").write_text("a\nb\nc\n", encoding="utf-8")
    result = _call(_registry(workspace), "read_file", {"path": "corpus/short.md",
                                                       "start_line": 99})
    assert not result.ok and "start_line 99 is past the end (3 lines)" in result.content


# --------------------------------------------------------------------------- R2-402/403/413
def test_grep_says_when_the_file_cap_hid_part_of_the_search(workspace, monkeypatch) -> None:
    import panda_core.tools as tools

    monkeypatch.setattr(tools, "GREP_FILE_CAP", 5)
    corpus = workspace.root / "corpus"
    for n in range(4):
        (corpus / f"dir{n}").mkdir()  # directories no longer use the cap
    for n in range(8):
        (corpus / f"f{n}.md").write_text("NEEDLE\n" if n == 7 else "hay\n", encoding="utf-8")
    result = _call(_registry(workspace), "grep", {"pattern": "NEEDLE", "glob": "corpus/**/*"})
    assert result.data["count"] == 0
    assert result.data["search_complete"] is False and result.data["file_cap_reached"] is True
    assert "narrow glob" in result.data["next_step"]
    assert result.data["files_scanned"] == 5


def test_grep_reports_a_complete_search(workspace) -> None:
    (workspace.root / "corpus" / "a.md").write_text("NEEDLE\n", encoding="utf-8")
    result = _call(_registry(workspace), "grep", {"pattern": "NEEDLE"})
    assert result.data["search_complete"] is True and "next_step" not in result.data


def test_list_files_says_when_more_entries_exist(workspace) -> None:
    for n in range(7):
        (workspace.root / "corpus" / f"x{n}.md").write_text("x", encoding="utf-8")
    result = _call(_registry(workspace), "list_files", {"pattern": "corpus/x*", "limit": 5})
    assert result.data["count"] == 5 and result.data["truncated"] is True
    whole = _call(_registry(workspace), "list_files", {"pattern": "corpus/x*", "limit": 7})
    assert whole.data["truncated"] is False


def _claims(workspace):
    claims = workspace.root / "corpus" / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / "c-open.md").write_text("---\nclaim_id: c-open\n---\nbody\n", encoding="utf-8")
    (claims / "c-secret.md").write_text("---\nclaim_id: c-secret\n---\nx\n", encoding="utf-8")
    policy = json.loads((workspace.root / ".panda" / "policy.json").read_text("utf-8"))
    policy["rules"].append({"id": "deny-secret-claim", "effect": "deny",
                               "actions": ["read"], "resources": ["corpus/claims/c-secret.md"],
                               "reason": "test"})
    (workspace.root / ".panda" / "policy.json").write_text(json.dumps(policy), "utf-8")


def _workspace_registry(workspace):
    from panda_core.policy import engine_for, load_policy_or_default
    from panda_core.tools import ToolContext, ToolRegistry

    policy, from_file = load_policy_or_default(workspace.policy_path)
    engine = engine_for(workspace, policy, from_file=from_file)
    return ToolRegistry(ToolContext(workspace=workspace, policy=engine))


def test_claim_list_counts_cards_hidden_by_policy(workspace) -> None:
    _claims(workspace)
    result = _call(_workspace_registry(workspace), "claim_list", {})
    assert result.data["total"] == 1 and result.data["omitted_by_policy"] == 1


def test_claim_show_of_a_denied_card_is_blocked_not_missing(workspace) -> None:
    _claims(workspace)
    result = _call(_workspace_registry(workspace), "claim_show", {"claim_id": "c-secret"})
    assert result.blocked and result.data["rule"] == "deny-secret-claim"
    assert "x\n" not in result.content


def test_evidence_list_total_counts_the_filtered_records(workspace) -> None:
    lines = [{"evidence_id": f"e{n}", "study_id": "s1" if n < 2 else "s2"} for n in range(5)]
    evidence = workspace.root / "corpus" / "evidence" / "evidence.jsonl"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    result = _call(_registry(workspace), "evidence_list", {"study_id": "s1"})
    assert result.data["total"] == 2 and result.data["records_in_file"] == 5


# --------------------------------------------------------------------------- R2-412
def test_a_policy_denial_says_not_to_retry_and_names_the_rule(workspace) -> None:
    result = _call(_registry(workspace), "read_file", {"path": ".env"})
    assert result.blocked
    step = result.data["next_step"]
    assert "Do not retry" in step and result.data["rule"] in step


def test_a_workspace_denial_points_at_the_workspace_policy(workspace) -> None:
    _claims(workspace)
    result = _call(_workspace_registry(workspace), "read_file",
                   {"path": "corpus/claims/c-secret.md"})
    assert result.blocked and ".panda/policy.json" in result.data["next_step"]


def test_no_approver_tells_the_user_to_approve_interactively(workspace) -> None:
    from panda_core.policy import PolicyEngine, default_policy
    from panda_core.tools import ToolContext, ToolRegistry

    registry = ToolRegistry(ToolContext(workspace=workspace,
                                        policy=PolicyEngine(default_policy(),
                                                            root=workspace.root)))
    result = _call(registry, "run_shell", {"argv": ["python", "-c", "print(1)"]})
    assert result.blocked and "no interactive approver" in result.data["detail"]
    assert "panda chat" in result.data["next_step"]


@pytest.mark.parametrize(
    ("auto", "approver", "detail", "hint"),
    [
        (True, None, "--auto does not approve", "--auto only approves"),
        (False, "no", "approval denied by the operator", "ask the user why"),
    ],
)
def test_a_denied_approval_says_who_denied_it(workspace, auto, approver, detail, hint) -> None:
    import asyncio

    from panda_cli.runtime import ChatRuntime

    async def no(request) -> bool:
        return False

    runtime = ChatRuntime(workspace, auto=auto, approver=no if approver else None)
    try:
        # route through the agent's approver, which records who decided
        runtime.tools.ctx.approve = runtime.agent._approve
        result = asyncio.run(runtime.tools.call(
            "run_shell", {"argv": ["python", "-c", "print(1)"]}))
    finally:
        runtime.close()
    assert result.blocked and detail in result.data["detail"]
    assert hint in result.data["next_step"]


# --------------------------------------------------------------------------- R2-410 / DEAD-001
def test_policy_check_offers_only_enforced_actions(workspace) -> None:
    from panda_core.policy import ENFORCED_ACTIONS

    registry = _registry(workspace)
    spec = next(s for s in registry.describe() if s["name"] == "policy_check")
    assert set(spec["parameters"]["properties"]["action"]["enum"]) == set(ENFORCED_ACTIONS)
    refused = _call(registry, "policy_check", {"action": "release", "resource": "build x"})
    assert not refused.ok and "invalid arguments" in refused.content
    checked = _call(registry, "policy_check", {"action": "validate", "resource": "."})
    assert checked.ok and checked.data["enforced"] is True
    assert checked.data["enforced_by"] == ["validate"]


def test_service_policy_check_says_when_an_action_is_not_enforced(workspace) -> None:
    from panda_core.service import PandaService

    service = PandaService(workspace)
    try:
        assert service.policy_check("release", "publish x")["enforced"] is False
        assert service.policy_check("read", "corpus/a.md")["enforced"] is True
    finally:
        close = getattr(service, "close", None)
        if close:
            close()


def test_dead_rule_families_are_gone_and_old_files_need_no_trust(tmp_path) -> None:
    from panda_core.policy import default_policy, load_policy
    from panda_core.trust import loosening_rules

    ids = {rule.id for rule in default_policy().rules}
    assert not ids & {"git-default-ask", "git-readonly", "git-push", "git-force-push",
                      "release-build", "spend-default-ask"}
    assert "release-publish" in ids  # the CONTRACT's planned hard deny stays
    legacy_file = tmp_path / "policy.json"
    legacy_file.write_text(json.dumps({"schema_version": 1, "default": "ask", "rules": [
        {"id": "git-readonly", "actions": ["git"],
         "resources": ["status*", "diff*", "log*", "show*", "branch*"], "effect": "allow"},
        {"id": "release-build", "actions": ["release"], "resources": ["build*"],
         "effect": "allow"},
    ]}), encoding="utf-8")
    assert loosening_rules(load_policy(legacy_file)) == []


# --------------------------------------------------------------------------- R2-411
GOLDEN = __import__("pathlib").Path(__file__).parents[1] / "golden" / "tools.json"


def test_every_tool_and_parameter_has_a_description(workspace) -> None:
    for spec in _registry(workspace).describe():
        assert len(spec["description"]) >= 40, spec["name"]
        for name, schema in spec["parameters"].get("properties", {}).items():
            assert schema.get("description"), f"{spec['name']}.{name}"


def test_tool_descriptions_match_the_golden_file(workspace) -> None:
    """Changing what the model is told is a reviewed change: update the golden file."""

    current = json.dumps(_registry(workspace).describe(), indent=2, sort_keys=True) + "\n"
    if not GOLDEN.exists() or __import__("os").environ.get("PANDA_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(current, encoding="utf-8", newline="\n")
    assert GOLDEN.read_text(encoding="utf-8") == current


# --------------------------------------------------------------------------- R2-414
def _agent(workspace, script, **kwargs):
    from tests.test_agent import make_agent

    return make_agent(workspace, script, **kwargs)


def test_a_turn_out_of_steps_still_ends_with_a_summary(workspace) -> None:
    import asyncio

    from panda_core.provider import DoneEvent, TextDelta, ToolCall

    call = ToolCall(id="c1", name="read_file", arguments='{"path":"README.md"}')
    loop = [DoneEvent(finish_reason="tool_calls", tool_calls=(call,))]
    summary = [TextDelta("did X; Y is left"),
               DoneEvent(finish_reason="tool_calls", tool_calls=(call,))]
    agent, session, provider = _agent(workspace, [loop, loop, summary], max_steps=2)
    result = asyncio.run(agent.run_turn("go"))
    assert result.status == "max_steps" and result.steps == 2
    assert "did X; Y is left" in result.text
    # the model was told before its last step and before the summary
    assert "last step of the turn that can run tools" in provider.calls[1][-1]["content"]
    assert "no tool calls will be run" in provider.calls[2][-1]["content"]
    text = session.path.read_text(encoding="utf-8")
    assert text.count('"type":"tool.result"') == 2  # the summary's call was not run
    assert '"dropped_tool_calls":1' in text and '"type":"harness.note"' in text


def test_max_tokens_stops_before_the_next_model_call(workspace) -> None:
    import asyncio

    from panda_core.provider import DoneEvent, ToolCall, Usage, UsageEvent

    call = ToolCall(id="c1", name="read_file", arguments='{"path":"README.md"}')
    step = [UsageEvent(Usage(prompt_tokens=900, completion_tokens=200, total_tokens=1100)),
            DoneEvent(finish_reason="tool_calls", tool_calls=(call,))]
    agent, _, provider = _agent(workspace, [step, step, step], max_steps=10)
    agent.max_tokens = 2000
    result = asyncio.run(agent.run_turn("go"))
    assert result.status == "budget_exceeded" and len(provider.calls) == 2


def test_budget_exceeded_is_a_limit_exit_code() -> None:
    from panda_cli.main import run_exit_code

    assert run_exit_code("budget_exceeded") == 7


def test_run_accepts_budget_options() -> None:
    from typer.testing import CliRunner

    from panda_cli.main import app

    out = CliRunner().invoke(app, ["run", "--help"], env={"COLUMNS": "200"}).output
    assert "--max-tokens" in out and "--max-cost" in out


# --------------------------------------------------------------------------- R2-407
@pytest.mark.parametrize("command", [[], ["run"], ["chat"]])
def test_session_help_does_not_promise_the_conversation_back(command) -> None:
    from typer.testing import CliRunner

    from panda_cli.main import app

    result = CliRunner().invoke(app, [*command, "--help"], env={"COLUMNS": "300"})
    assert result.exit_code == 0
    assert "Continue" not in result.output
    assert "without that session's conversation" in " ".join(result.output.split())
