"""Wave 7: a workspace policy only tightens unless trusted; rules match argv tokens.

R2-113, R2-207, R2-116, R2-100, R2-104, R2-103, R2-203, R2-317, R2-105, R2-210.
Most bypass cases live in tests/evals/policy/cases; these pin the loader and the engine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panda_core.errors import ConfigError
from panda_core.policy import PolicyEngine, default_policy, engine_for, load_policy


def _write_policy(path: Path, rules: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "default": "ask", "rules": rules}), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"id": "a", "actions": "shell", "resources": ["*"], "effect": "ask"}, "list of strings"),
        ({"id": "a", "actions": ["shell"], "resources": "*", "effect": "ask"}, "list of strings"),
        ({"id": "a", "actions": [], "resources": ["*"], "effect": "ask"}, "list of strings"),
        ({"id": "a", "actions": [1], "resources": ["*"], "effect": "ask"}, "list of strings"),
        ({"id": "a", "actions": ["shell"], "resources": ["*"], "effect": "ask",
          "risk": "\x1b[2Jnone"}, "invalid risk"),
        ({"id": "a", "actions": ["shell"], "resources": ["*"], "effect": "ask",
          "allow_everything": True}, "unknown fields"),
        ({"id": 7, "actions": ["shell"], "resources": ["*"], "effect": "ask"}, "must be strings"),
        ("not an object", "must be an object"),
    ],
)
def test_malformed_policy_rules_are_refused(tmp_path: Path, rule, message: str) -> None:
    path = _write_policy(tmp_path / ".panda" / "policy.json", [rule])
    with pytest.raises(ConfigError, match=message):
        load_policy(path)


def test_the_built_in_policy_round_trips_through_the_strict_loader(tmp_path: Path) -> None:
    path = _write_policy(
        tmp_path / ".panda" / "policy.json", default_policy().to_dict()["rules"]
    )
    assert load_policy(path).rules == default_policy().rules


def test_untrusted_workspace_policy_only_tightens(workspace) -> None:
    _write_policy(
        workspace.policy_path,
        [
            {"id": "read-default", "actions": ["read"], "resources": ["*"], "effect": "allow"},
            {"id": "corpus-write", "actions": ["write"], "resources": ["*"], "effect": "allow"},
            {"id": "corpus-deny", "actions": ["read"], "resources": ["corpus/private/*"],
             "effect": "deny", "risk": "high"},
        ],
    )
    policy = load_policy(workspace.policy_path)
    engine = engine_for(workspace, policy, from_file=True)
    assert engine.decide("write", "README.md").effect == "deny"  # built-in wins
    assert engine.decide("read", "corpus/private/x.md").effect == "deny"  # tightening works
    assert engine.decide("read", "corpus/open.md").effect == "allow"


def test_trusting_the_file_lets_its_allow_rules_apply(workspace) -> None:
    from panda_core.trust import grant, is_trusted

    _write_policy(
        workspace.policy_path,
        [{"id": "corpus-write", "actions": ["write"], "resources": ["*"], "effect": "allow"}],
    )
    grant(workspace.root, workspace.policy_path)
    assert is_trusted(workspace.root, workspace.policy_path)
    engine = engine_for(workspace, load_policy(workspace.policy_path), from_file=True)
    assert engine.decide("write", "README.md").effect == "allow"
    assert engine.decide("write", ".panda/policy.json").effect == "hard_deny"  # floor
    assert engine.decide("write", ".git/hooks/pre-commit").effect == "hard_deny"  # floor


def test_configured_validator_outside_the_default_dir_is_not_agent_writable(workspace) -> None:
    from types import SimpleNamespace

    fake = SimpleNamespace(
        root=workspace.root,
        policy_path=workspace.policy_path,
        config=SimpleNamespace(validator="tools/check/validate.mjs"),
    )
    engine = engine_for(fake, default_policy(), from_file=False)
    assert engine.decide("write", "tools/check/validate.mjs").effect == "hard_deny"
    assert engine.decide("write", "tools/check/helper.mjs").effect == "hard_deny"
    assert engine.decide("write", "tools/other.txt").effect != "hard_deny"


def test_a_validator_at_the_root_does_not_freeze_every_write(workspace) -> None:
    from types import SimpleNamespace

    fake = SimpleNamespace(
        root=workspace.root, policy_path=workspace.policy_path,
        config=SimpleNamespace(validator="validate.mjs"),
    )
    engine = engine_for(fake, default_policy(), from_file=False)
    assert engine.decide("write", "validate.mjs").effect == "hard_deny"
    assert engine.decide("write", "corpus/claims/c1.md").effect == "allow"


@pytest.mark.parametrize(
    "command",
    ['git "push" --force', '"git" push --force', 'git "reset" --hard', 'git push origin "+main"'],
)
def test_string_and_argv_routes_agree_on_quoted_commands(command: str) -> None:
    # R2-203/R2-503: the string route used posix=False on Windows and kept quotes.
    assert PolicyEngine(default_policy()).decide("shell", command).effect == "hard_deny"


@pytest.mark.parametrize(
    "argv",
    [["git.exe.", "push", "--force"], ["git.exe ", "reset", "--hard"],
     ["GIT~1.EXE", "clean", "-fdx"], ["git.exe::$DATA", "push", "--force"],
     ["git.com", "push", "--force"], ["rm.exe.", "-rf", "x"]],
)
def test_windows_program_spellings_keep_hard_deny(argv: list[str]) -> None:
    assert PolicyEngine(default_policy()).decide_shell(argv).effect == "hard_deny"


def test_classifier_rule_and_risk_survive_a_tie_with_the_catch_all() -> None:
    # R2-500 root cause: on equal effect the base rule (shell-default-ask) used to win.
    decision = PolicyEngine(default_policy()).decide_shell(["env", "git", "push", "origin", "x"])
    assert decision.effect == "ask"
    assert decision.rule_id == "shell-git-push" and decision.risk == "high"
