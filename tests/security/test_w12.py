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
