"""`panda init`: idempotent scaffolding, backups, and OpenCode wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panda_core.errors import ConfigError
from panda_core.init import initialize_workspace
from panda_core.workspace import Workspace


def test_init_creates_contract(fake_corpus: Path) -> None:
    result = initialize_workspace(fake_corpus)
    assert not result.dry_run
    assert (fake_corpus / ".panda" / "config.json").is_file()
    assert (fake_corpus / ".panda" / "policy.json").is_file()
    assert (fake_corpus / ".panda" / "instructions.md").is_file()
    assert (fake_corpus / ".panda" / "sessions").is_dir()
    assert not (fake_corpus / "opencode.json").exists()
    assert (fake_corpus / ".panda" / ".gitignore").is_file()
    gitignore = (fake_corpus / ".panda" / ".gitignore").read_text(encoding="utf-8")
    assert "sessions/" in gitignore
    workspace = Workspace.from_root(fake_corpus)
    assert workspace.initialized
    assert workspace.config.model == "opencode-go/deepseek-v4.1-flash"
    assert workspace.config.provider.api_key_env == "OPENCODE_API_KEY"


def test_init_dry_run_writes_nothing(fake_corpus: Path) -> None:
    result = initialize_workspace(fake_corpus, dry_run=True)
    assert result.dry_run
    assert not (fake_corpus / ".panda").exists()
    assert not (fake_corpus / "opencode.json").exists()
    assert any("dry run" in warning for warning in result.warnings)


def test_init_is_idempotent(fake_corpus: Path) -> None:
    initialize_workspace(fake_corpus)
    policy_before = (fake_corpus / ".panda" / "policy.json").read_bytes()
    instructions_before = (fake_corpus / ".panda" / "instructions.md").read_bytes()
    second = initialize_workspace(fake_corpus)
    assert (fake_corpus / ".panda" / "policy.json").read_bytes() == policy_before
    assert (fake_corpus / ".panda" / "instructions.md").read_bytes() == instructions_before
    assert str(fake_corpus / ".panda" / "policy.json") in second.unchanged
    assert str(fake_corpus / ".panda" / "instructions.md") in second.unchanged


def test_init_force_backs_up_policy(fake_corpus: Path) -> None:
    initialize_workspace(fake_corpus)
    policy = fake_corpus / ".panda" / "policy.json"
    policy.write_text('{"schema_version": 1, "default": "allow", "rules": []}', encoding="utf-8")
    initialize_workspace(fake_corpus, force=True)
    backups = list((fake_corpus / ".panda" / "backups").rglob("policy.json"))
    assert backups
    assert any("allow" in backup.read_text(encoding="utf-8") for backup in backups)
    assert '"effect": "allow"' in policy.read_text(encoding="utf-8")


def test_init_requires_validator(fake_corpus: Path) -> None:
    (fake_corpus / "corpus" / "validation" / "validate.mjs").unlink()
    with pytest.raises(ConfigError):
        initialize_workspace(fake_corpus)
    result = initialize_workspace(fake_corpus, allow_missing_validator=True)
    assert any("validator is missing" in warning for warning in result.warnings)


def test_init_wires_opencode(wired_workspace: Workspace) -> None:
    config_path = wired_workspace.opencode_config_path
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["$schema"] == "https://opencode.ai/config.json"
    assert payload["default_agent"] == "panda"
    assert payload["model"] == wired_workspace.config.model
    server = payload["mcp"]["servers"]["panda"]
    assert server["type"] == "local"
    assert server["command"][-4:] == ["mcp", "serve", "--root", str(wired_workspace.root)]
    assert server["cwd"] == str(wired_workspace.root)
    assert server["environment"]["PANDA_ROOT"] == str(wired_workspace.root)
    assert payload["agents"]["panda"]["mode"] == "primary"
    assert payload["agents"]["panda"]["description"]
    actions = [rule["action"] for rule in payload["permissions"]]
    assert "panda_validate" in actions
    assert "panda_study_show" in actions


def test_init_preserves_existing_opencode_config(fake_corpus: Path) -> None:
    existing = {
        "$schema": "https://opencode.ai/config.json",
        "model": "openrouter/anthropic/claude-opus-5.5",
        "theme": "custom-theme",
        "instructions": ["NOTES.md"],
    }
    (fake_corpus / "opencode.json").write_text(json.dumps(existing), encoding="utf-8")
    result = initialize_workspace(fake_corpus, opencode=True, model="opencode/glm-5.3")
    payload = json.loads((fake_corpus / "opencode.json").read_text(encoding="utf-8"))
    assert payload["model"] == "openrouter/anthropic/claude-opus-5.5"
    assert payload["theme"] == "custom-theme"
    assert "NOTES.md" in payload["instructions"]
    assert ".panda/instructions.md" in payload["instructions"]
    assert any("kept existing model" in warning for warning in result.warnings)
    assert list((fake_corpus / ".panda" / "backups").rglob("opencode.json"))


def test_init_force_model(fake_corpus: Path) -> None:
    (fake_corpus / "opencode.json").write_text(
        json.dumps({"model": "openrouter/anthropic/claude-opus-5.5"}), encoding="utf-8"
    )
    initialize_workspace(fake_corpus, opencode=True, model="opencode/glm-5.3", force_model=True)
    payload = json.loads((fake_corpus / "opencode.json").read_text(encoding="utf-8"))
    assert payload["model"] == "opencode/glm-5.3"


def test_init_refuses_jsonc(fake_corpus: Path) -> None:
    (fake_corpus / "opencode.jsonc").write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        initialize_workspace(fake_corpus, opencode=True)
    assert not (fake_corpus / ".panda" / "config.json").exists()


def test_init_targets_nested_opencode_config(fake_corpus: Path) -> None:
    nested = fake_corpus / ".opencode" / "opencode.json"
    nested.parent.mkdir()
    nested.write_text(json.dumps({"theme": "nested"}), encoding="utf-8")
    initialize_workspace(fake_corpus, opencode=True)
    payload = json.loads(nested.read_text(encoding="utf-8"))
    assert payload["theme"] == "nested"
    assert "panda" in payload["mcp"]["servers"]
    workspace = Workspace.from_root(fake_corpus)
    assert workspace.config.opencode.config_path == ".opencode/opencode.json"


def test_init_dry_run_does_not_touch_existing_opencode(fake_corpus: Path) -> None:
    target = fake_corpus / "opencode.json"
    target.write_text('{"model": "openrouter/keep/me"}', encoding="utf-8")
    before = target.read_bytes()
    result = initialize_workspace(fake_corpus, opencode=True, dry_run=True)
    assert target.read_bytes() == before
    assert str(target) in result.updated


def test_init_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        initialize_workspace(tmp_path / "missing")
