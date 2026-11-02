"""Wave 1: policy by effect (SEC-001/002/003/005/007/010/011, MCP-001/002, POL-001).

The policy must decide the effect on a resolved resource, not the text of a
command. Everything runs on synthetic data inside `tmp_path`.
"""

from __future__ import annotations

import json
import sys

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


# --------------------------------------------------------------------------- helpers (tools)
def make_registry(workspace, *, approver=None):
    from panda_core.policy import load_policy_or_default
    from panda_core.session import SessionLog
    from panda_core.tools import ToolContext, ToolRegistry

    session = SessionLog.create(workspace.sessions_dir, label="w1")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
        approve=approver,
    )
    return ToolRegistry(ctx), session


def call(registry, name: str, **arguments):
    import asyncio

    return asyncio.run(registry.call(name, arguments))


SYNTH = "SYNTH_SECRET_GREP_VALUE_7c1d"


def _plant_secrets(workspace: Workspace) -> None:
    root = workspace.root
    (root / ".env").write_text(f"TOKEN={SYNTH}\n", encoding="utf-8")
    (root / "deploy.pem").write_text(f"-----BEGIN-----{SYNTH}\n", encoding="utf-8")
    (root / ".npmrc").write_text(f"//registry/:_authToken={SYNTH}\n", encoding="utf-8")
    (root / "corpus" / "visible.md").write_text(f"public {SYNTH}-visible\n", encoding="utf-8")


# --------------------------------------------------------------------------- SEC-003
@pytest.mark.parametrize("glob", ["**/*", ".env", "*.pem", ".*", "**/.npmrc"])
def test_grep_never_returns_lines_from_files_read_file_would_refuse(
    workspace: Workspace, glob: str
) -> None:
    _plant_secrets(workspace)
    registry, _ = make_registry(workspace)
    result = call(registry, "grep", pattern=SYNTH, glob=glob, fixed_strings=True)
    text = result.content
    assert ".env" not in "".join(result.data.get("matches", []))
    assert "deploy.pem" not in text.replace("omitted", "")
    assert "_authToken" not in text
    assert "TOKEN=" not in text


def test_grep_still_finds_ordinary_files_and_reports_omissions(workspace: Workspace) -> None:
    _plant_secrets(workspace)
    registry, _ = make_registry(workspace)
    result = call(registry, "grep", pattern=SYNTH, fixed_strings=True)
    assert result.ok
    assert any("visible.md" in line for line in result.data["matches"])
    assert result.data["omitted_by_policy"] >= 3


def test_list_files_omits_what_read_file_refuses(workspace: Workspace) -> None:
    _plant_secrets(workspace)
    registry, _ = make_registry(workspace)
    result = call(registry, "list_files", pattern="*")
    entries = result.data["entries"]
    assert ".env" not in entries and "deploy.pem" not in entries and ".npmrc" not in entries
    assert "README.md" in entries
    assert result.data["omitted_by_policy"] >= 3


def test_search_tools_never_prompt_per_file(workspace: Workspace) -> None:
    """A file that would need approval is omitted, not asked about."""

    asked: list[object] = []

    async def approver(request: object) -> bool:
        asked.append(request)
        return True

    body = {
        "schema_version": 1,
        "default": "ask",
        "rules": [
            {"id": "ro", "actions": ["read"], "resources": ["*"], "effect": "allow"},
            {"id": "ask-notes", "actions": ["read"], "resources": ["notes/*"], "effect": "ask"},
        ],
    }
    workspace.policy_path.write_text(json.dumps(body), encoding="utf-8")
    (workspace.root / "notes").mkdir()
    (workspace.root / "notes" / "n.md").write_text(SYNTH, encoding="utf-8")
    registry, _ = make_registry(workspace, approver=approver)
    result = call(registry, "grep", pattern=SYNTH, fixed_strings=True)
    assert not any("notes/" in line for line in result.data["matches"])
    assert asked == []


def test_spy_every_search_hit_goes_through_decide(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_secrets(workspace)
    seen: list[tuple[str, str]] = []
    real = PolicyEngine.decide

    def spy(self, action, resource):
        seen.append((action, resource))
        return real(self, action, resource)

    monkeypatch.setattr(PolicyEngine, "decide", spy)
    registry, _ = make_registry(workspace)
    call(registry, "grep", pattern=SYNTH, fixed_strings=True)
    resources = {resource for action, resource in seen if action == "read"}
    assert {".env", "deploy.pem", "corpus/visible.md"} <= resources


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")
def test_grep_judges_a_symlink_by_its_target(workspace: Workspace) -> None:
    _plant_secrets(workspace)
    (workspace.root / "corpus" / "innocent.md").symlink_to(workspace.root / ".env")
    registry, _ = make_registry(workspace)
    for tool, args in (
        ("grep", {"pattern": SYNTH, "fixed_strings": True}),
        ("list_files", {"pattern": "corpus/*"}),
    ):
        result = call(registry, tool, **args)
        assert "innocent.md" not in result.content
        assert "TOKEN=" not in result.content
