"""Wave 1: policy by effect (SEC-001/002/003/005/007/010/011, MCP-001/002, POL-001).

The policy must decide the effect on a resolved resource, not the text of a
command. Everything runs on synthetic data inside `tmp_path`.
"""

from __future__ import annotations

import json

import pytest

from panda_core.errors import ConfigError
from panda_core.policy import PolicyEngine, default_policy, load_policy
from panda_core.workspace import Workspace


def engine() -> PolicyEngine:
    return PolicyEngine(default_policy())


# --------------------------------------------------------------------------- POL-001
@pytest.mark.parametrize("effect", ["hard-deny", "HARD_DENY", "Allow", "block", ""])
def test_unknown_effect_is_a_config_error(workspace: Workspace, effect: str) -> None:
    body = {
        "schema_version": 1,
        "default": "ask",
        "rules": [{"id": "x", "actions": ["read"], "resources": ["*"], "effect": effect}],
    }
    workspace.policy_path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_policy(workspace.policy_path)


@pytest.mark.parametrize("effect", ["allow", "ask", "deny", "hard_deny"])
def test_known_effects_still_load(workspace: Workspace, effect: str) -> None:
    body = {
        "schema_version": 1,
        "default": "ask",
        "rules": [{"id": "x", "actions": ["read"], "resources": ["*"], "effect": effect}],
    }
    workspace.policy_path.write_text(json.dumps(body), encoding="utf-8")
    assert load_policy(workspace.policy_path).rules[0].effect == effect


# --------------------------------------------------------------------------- SEC-005
SECRET_PATHS = [
    ".git/config",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials.json",
    ".aws/credentials",
    ".ssh/config",
    ".docker/config.json",
    ".pgpass",
    "sub/dir/.npmrc",
    "sub/.git/config",
    "sub/credentials.json",
    "deploy/server.p12",
    "deploy/cert.pfx",
    "vault.kdbx",
    "app.keystore",
    "secrets.yaml",
    "config/secrets.json",
]


@pytest.mark.parametrize("path", SECRET_PATHS)
def test_credential_files_are_hard_denied_at_the_root_and_nested(path: str) -> None:
    assert engine().decide("read", path).effect == "hard_deny"


@pytest.mark.parametrize("path", ["README.md", "corpus/evidence.md", "docs/credentials-howto.md"])
def test_neg_ordinary_files_stay_readable(path: str) -> None:
    assert engine().decide("read", path).effect == "allow"
