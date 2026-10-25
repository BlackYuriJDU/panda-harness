"""Policy: last-match-wins, hard denies, and fail-closed approval."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panda_core.errors import ConfigError, PolicyDenied
from panda_core.policy import (
    PolicyEngine,
    default_policy,
    load_policy,
    load_policy_or_default,
    write_default_policy,
)


@pytest.fixture()
def engine() -> PolicyEngine:
    return PolicyEngine(default_policy())


def test_write_inside_corpus_is_allowed() -> None:
    engine = PolicyEngine(default_policy())
    assert engine.decide("write", "corpus/studies/gse160208.md").effect == "allow"
    assert engine.decide("edit", "memory/notes.md").effect == "allow"


def test_write_outside_scope_is_denied() -> None:
    engine = PolicyEngine(default_policy())
    assert engine.decide("write", "README.md").effect == "deny"
    assert engine.decide("write", "src/app.py").effect == "deny"


def test_review_scopes_require_approval() -> None:
    engine = PolicyEngine(default_policy())
    decision = engine.decide("write", "pipeline/run.py")
    assert decision.effect == "ask"
    assert decision.risk == "high"


def test_release_manifest_requires_approval() -> None:
    engine = PolicyEngine(default_policy())
    assert engine.decide("edit", "corpus/releases/v1/manifest.json").effect == "ask"


def test_secret_reads_are_hard_denied_even_with_approval() -> None:
    engine = PolicyEngine(default_policy())
    for resource in (".env", "corpus/.env.local", "keys/server.pem", "home/.ssh/id_rsa"):
        decision = engine.decide("read", resource)
        assert decision.effect == "hard_deny", resource
        with pytest.raises(PolicyDenied):
            engine.authorize("read", resource, approved=True, interactive=True)


def test_shell_readonly_allowed_and_push_asks() -> None:
    engine = PolicyEngine(default_policy())
    assert engine.decide("shell", "git status --short").effect == "allow"
    assert engine.decide("shell", "node corpus/validation/validate.mjs").effect == "allow"
    assert engine.decide("shell", "git push origin main").effect == "ask"


def test_force_push_and_destructive_commands_are_hard_denied() -> None:
    engine = PolicyEngine(default_policy())
    for command in (
        "git push origin main --force",
        "git push -f origin main",
        "git reset --hard HEAD~1",
        "rm -rf corpus",
        "Remove-Item -Recurse -Force corpus",
    ):
        assert engine.decide("shell", command).effect == "hard_deny", command


def test_release_publish_is_hard_denied() -> None:
    engine = PolicyEngine(default_policy())
    decision = engine.decide("release", "publish pypi")
    assert decision.effect == "hard_deny"


def test_ask_is_denied_when_non_interactive(engine: PolicyEngine) -> None:
    with pytest.raises(PolicyDenied) as excinfo:
        engine.authorize("shell", "cargo install something")
    assert excinfo.value.details["effect"] == "ask"


def test_ask_can_be_approved_once(engine: PolicyEngine) -> None:
    decision = engine.authorize("shell", "cargo install something", approved=True)
    assert decision.effect == "allow"
    assert "approved once" in decision.reason


def test_interactive_ask_is_returned_not_denied(engine: PolicyEngine) -> None:
    decision = engine.authorize("shell", "cargo install something", interactive=True)
    assert decision.effect == "ask"
    assert decision.requires_approval


def test_no_match_uses_default() -> None:
    policy = default_policy()
    custom = type(policy)(rules=policy.rules, default="deny")
    engine = PolicyEngine(custom)
    decision = engine.decide("teleport", "moon")
    assert decision.effect == "deny"
    assert decision.rule_id is None


def test_policy_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    write_default_policy(path)
    loaded = load_policy(path)
    assert loaded.to_dict() == default_policy().to_dict()


def test_policy_rejects_unknown_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"schema_version": 99, "rules": []}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_policy(path)


def test_policy_rejects_malformed_rules(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    payload = default_policy().to_dict()
    payload["rules"][0]["actions"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_policy(path)


def test_missing_policy_falls_back_to_default(tmp_path: Path) -> None:
    policy, from_file = load_policy_or_default(tmp_path / "missing.json")
    assert from_file is False
    assert len(policy.rules) == len(default_policy().rules)


def test_backslash_resources_are_normalized(engine: PolicyEngine) -> None:
    decision = engine.decide("write", "corpus\\studies\\x.md")
    assert decision.resource == "corpus/studies/x.md"
    assert decision.effect == "allow"
