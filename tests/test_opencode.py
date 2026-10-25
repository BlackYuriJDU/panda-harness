"""OpenCode V2 integration: config shape, permission order, launcher argv."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest

from panda_core.errors import ConfigError
from panda_core.opencode import (
    build_opencode_config,
    build_permissions,
    build_run_command,
    config_status,
    default_panda_command,
    dry_run_text,
    merge_opencode_config,
    resolve_config_target,
)
from panda_core.workspace import Workspace


def last_rule(rules: list[dict], action: str, resource: str) -> dict | None:
    matched = None
    for rule in rules:
        if fnmatch.fnmatchcase(action, rule["action"]) and fnmatch.fnmatchcase(
            resource, rule["resource"]
        ):
            matched = rule
    return matched


def test_config_uses_v2_shape(wired_workspace: Workspace) -> None:
    config = build_opencode_config(wired_workspace)
    assert config["$schema"] == "https://opencode.ai/config.json"
    assert isinstance(config["permissions"], list)
    assert "servers" in config["mcp"]
    assert "panda" in config["mcp"]["servers"]
    assert config["default_agent"] == "panda"
    assert config["instructions"] == [wired_workspace.config.instructions]
    assert config["agents"]["panda"]["mode"] == "primary"


def test_permission_denies_secrets_after_broad_read_allow() -> None:
    rules = build_permissions()
    assert last_rule(rules, "read", "corpus/notes.md")["effect"] == "allow"
    assert last_rule(rules, "read", ".env")["effect"] == "deny"
    assert last_rule(rules, "read", "home/.ssh/id_rsa")["effect"] == "deny"


def test_permission_write_scopes() -> None:
    rules = build_permissions()
    assert last_rule(rules, "edit", "corpus/studies/x.md")["effect"] == "allow"
    assert last_rule(rules, "edit", "memory/x.md")["effect"] == "allow"
    assert last_rule(rules, "edit", "corpus/releases/x.json")["effect"] == "ask"
    assert last_rule(rules, "edit", "src/app.py")["effect"] == "deny"


def test_permission_destructive_shell_is_denied() -> None:
    rules = build_permissions()
    assert last_rule(rules, "shell", "git status --short")["effect"] == "allow"
    assert last_rule(rules, "shell", "git push origin main")["effect"] == "ask"
    assert last_rule(rules, "shell", "git push origin main --force")["effect"] == "deny"
    assert last_rule(rules, "shell", "rm -rf corpus")["effect"] == "deny"


def test_permission_panda_tools_are_allowed() -> None:
    rules = build_permissions()
    for tool in (
        "panda_workspace_info",
        "panda_validate",
        "panda_policy_check",
        "panda_study_list",
        "panda_study_show",
        "panda_session_note",
    ):
        assert last_rule(rules, tool, "*")["effect"] == "allow"


def test_mcp_server_command_is_absolute(wired_workspace: Workspace) -> None:
    config = build_opencode_config(wired_workspace)
    server = config["mcp"]["servers"]["panda"]
    assert Path(server["cwd"]) == wired_workspace.root
    assert server["environment"]["PANDA_CONFIG"] == str(wired_workspace.config_path)
    assert "mcp" in server["command"]
    assert "serve" in server["command"]


def test_build_run_command_defaults(wired_workspace: Workspace) -> None:
    command = build_run_command(wired_workspace, "validate the corpus")
    assert "--standalone" in command
    assert command[-2:] == ["--", "validate the corpus"]
    assert wired_workspace.config.model in command
    assert "panda" in command


def test_build_run_command_overrides(wired_workspace: Workspace) -> None:
    command = build_run_command(
        wired_workspace,
        "task",
        model="openrouter/google/gemini-4-argon",
        agent="reviewer",
        output_format="json",
        title="Audit",
        standalone=False,
    )
    assert "--standalone" not in command
    assert command[command.index("--model") + 1] == "openrouter/google/gemini-4-argon"
    assert command[command.index("--agent") + 1] == "reviewer"
    assert command[command.index("--format") + 1] == "json"
    assert command[command.index("--title") + 1] == "Audit"


def test_build_run_command_rejects_bad_format(wired_workspace: Workspace) -> None:
    with pytest.raises(ConfigError):
        build_run_command(wired_workspace, "task", output_format="xml")


def test_merge_preserves_user_keys_and_adds_panda(wired_workspace: Workspace) -> None:
    generated = build_opencode_config(wired_workspace)
    existing = {
        "theme": "custom",
        "permissions": [{"action": "shell", "resource": "git push *", "effect": "deny"}],
    }
    merged, changes, warnings = merge_opencode_config(existing, generated)
    assert merged["theme"] == "custom"
    assert merged["permissions"][-1] == {
        "action": "shell",
        "resource": "git push *",
        "effect": "deny",
    }
    assert any("Panda" in change for change in changes)
    assert warnings == []


def test_merge_keeps_existing_model_unless_forced(wired_workspace: Workspace) -> None:
    generated = build_opencode_config(wired_workspace, model="openrouter/new/model")
    merged, _, warnings = merge_opencode_config(
        {"model": "openrouter/old/model"}, generated
    )
    assert merged["model"] == "openrouter/old/model"
    assert any("kept existing model" in warning for warning in warnings)

    merged, changes, _ = merge_opencode_config(
        {"model": "openrouter/old/model"}, generated, force_model=True
    )
    assert merged["model"] == "openrouter/new/model"
    assert any("changed model" in change for change in changes)


def test_merge_deduplicates_permissions(wired_workspace: Workspace) -> None:
    generated = build_opencode_config(wired_workspace)
    merged, _, _ = merge_opencode_config(generated, generated)
    keys = [(rule["action"], rule["resource"], rule["effect"]) for rule in merged["permissions"]]
    assert len(keys) == len(set(keys))


def test_merge_rejects_non_list_permissions(wired_workspace: Workspace) -> None:
    with pytest.raises(ConfigError):
        merge_opencode_config(
            {"permissions": "allow"}, build_opencode_config(wired_workspace)
        )


def test_resolve_config_target_prefers_nested(wired_workspace: Workspace) -> None:
    nested = wired_workspace.root / ".opencode" / "opencode.json"
    nested.parent.mkdir(exist_ok=True)
    nested.write_text("{}", encoding="utf-8")
    assert resolve_config_target(wired_workspace) == nested


def test_resolve_config_target_refuses_jsonc(workspace: Workspace) -> None:
    (workspace.root / "opencode.jsonc").write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        resolve_config_target(workspace)


def test_config_status(wired_workspace: Workspace) -> None:
    status = config_status(wired_workspace)
    assert status["exists"] is True
    assert status["ok"] is True
    assert status["panda_mcp"] is True
    assert status["default_agent"] == "panda"
    assert status["permission_rules"] > 0


def test_default_panda_command_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANDA_BIN", "python -m panda_cli")
    assert default_panda_command() == ["python", "-m", "panda_cli"]


def test_dry_run_text_is_single_line(wired_workspace: Workspace) -> None:
    command = build_run_command(wired_workspace, "task with spaces")
    text = dry_run_text(command)
    assert "task with spaces" in text


def test_config_file_is_valid_json(wired_workspace: Workspace) -> None:
    payload = json.loads(wired_workspace.opencode_config_path.read_text(encoding="utf-8"))
    assert payload["agents"]["panda"]["system"]
