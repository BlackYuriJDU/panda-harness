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
