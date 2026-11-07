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


# --------------------------------------------------------------------------- SEC-010 / SEC-011
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")
MARK = "OUTSIDE_SYNTH_SECRET_STUDY_55e2"


def _outside(tmp_path) -> Path:  # noqa: F821
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(f"---\nstudy_id: gse160208\n---\n{MARK}\n", encoding="utf-8")
    return outside


def _repoint_index(workspace: Workspace, target: str) -> None:
    index = workspace.root / "corpus" / "studies" / "INDEX.md"
    text = index.read_text(encoding="utf-8").replace("(gse160208.md)", f"({target})")
    index.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("variant", ["absolute", "dotdot", "dotdot-deep"])
def test_study_show_refuses_an_index_link_that_leaves_the_studies_dir(
    workspace: Workspace, tmp_path, variant: str
) -> None:
    outside = _outside(tmp_path)
    target = {
        "absolute": str(outside / "secret.md"),
        "dotdot": "../../../outside/secret.md",
        "dotdot-deep": "sub/../../../../outside/secret.md",
    }[variant]
    _repoint_index(workspace, target)
    registry, _ = make_registry(workspace)
    result = call(registry, "study_show", study_id="gse160208")
    assert not result.ok
    assert MARK not in result.content


@POSIX_ONLY
def test_study_show_refuses_a_symlinked_card(workspace: Workspace, tmp_path) -> None:
    outside = _outside(tmp_path)
    card = workspace.root / "corpus" / "studies" / "gse160208.md"
    card.unlink()
    card.symlink_to(outside / "secret.md")
    registry, _ = make_registry(workspace)
    result = call(registry, "study_show", study_id="gse160208")
    assert not result.ok
    assert MARK not in result.content


def test_study_show_authorizes_the_card_path(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []
    real = PolicyEngine.decide

    def spy(self, action, resource):
        seen.append((action, resource))
        return real(self, action, resource)

    monkeypatch.setattr(PolicyEngine, "decide", spy)
    registry, _ = make_registry(workspace)
    call(registry, "study_show", study_id="gse160208")
    assert ("read", "corpus/studies/gse160208.md") in seen


def test_neg_study_show_still_reads_a_regular_card(workspace: Workspace) -> None:
    registry, _ = make_registry(workspace)
    result = call(registry, "study_show", study_id="gse160208")
    assert result.ok and "GSE160208" in result.content


@POSIX_ONLY
def test_claim_tools_ignore_a_symlink_to_a_file_outside(workspace: Workspace, tmp_path) -> None:
    outside = _outside(tmp_path)
    claims = workspace.root / "corpus" / "claims"
    claims.mkdir(exist_ok=True)
    (claims / "real.md").write_text("---\nclaim_id: real\n---\nok\n", encoding="utf-8")
    (claims / "leak.md").symlink_to(outside / "secret.md")
    registry, _ = make_registry(workspace)
    for tool, args in (("claim_list", {}), ("claim_show", {"claim_id": "gse160208"})):
        assert MARK not in call(registry, tool, **args).content
    listing = call(registry, "claim_list")
    assert listing.ok and listing.data["total"] == 1


# --------------------------------------------------------------------------- SEC-001 / SEC-007
def decide_shell(command: str) -> str:
    return engine().decide("shell", command).effect


# The nine decisions the audit measured (F04), with the effect they must have now.
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git push origin fix-foo", "ask"),
        ("git -C . reset --hard HEAD", "hard_deny"),
        ("git -c core.pager=cat push --force origin main", "hard_deny"),
        ("rm -r -f corpus", "hard_deny"),
        ("find corpus -delete", "hard_deny"),
        ("git push origin +main", "hard_deny"),
        ("git branch -D main", "hard_deny"),
        ("git diff --output=README.md", "deny"),
        ("git log -p --output=x", "deny"),
    ],
)
def test_f04_decisions_by_effect(command: str, expected: str) -> None:
    assert decide_shell(command) == expected


# Reformulations of the same effect (option order, '=' vs space, ./ and case).
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git --no-pager -C . push -f origin main", "hard_deny"),
        ("git push -fu origin main", "hard_deny"),
        ("git push --force-with-lease origin main", "hard_deny"),
        ("git push --for origin main", "hard_deny"),
        ("git push origin :main", "hard_deny"),
        ("git push --delete origin topic", "hard_deny"),
        ("git push -d origin topic", "hard_deny"),
        ("git push --mirror", "hard_deny"),
        ("git push --mir", "hard_deny"),
        ("GIT.EXE push --force origin main", "hard_deny"),
        ("./git push --force", "hard_deny"),
        ("/usr/bin/git reset --hard", "hard_deny"),
        ("git reset --ha HEAD~1", "hard_deny"),
        ("git clean -f", "hard_deny"),
        ("git clean -fd", "hard_deny"),
        ("git clean -fx", "hard_deny"),
        ("git clean --force -d", "hard_deny"),
        ("git branch --delete topic", "hard_deny"),
        ("git branch -d topic", "hard_deny"),
        ("env git push --force", "hard_deny"),
        ("rm -fr corpus", "hard_deny"),
        ("rm -Rf corpus", "hard_deny"),
        ("rm --recursive --force corpus", "hard_deny"),
        ("RM -R corpus", "hard_deny"),
        ("find . -type f -delete", "hard_deny"),
        ("git diff --out=README.md", "deny"),
        ("git diff --output README.md", "deny"),
        ("git diff --no-index /dev/null /etc/hosts", "deny"),
        ("git diff --ext-diff", "deny"),
        ("git show --output=x HEAD", "deny"),
    ],
)
def test_destructive_and_exfiltrating_variants_fail_closed(command: str, expected: str) -> None:
    assert decide_shell(command) == expected


# R2-503 / M22: shlex posix=True must strip quotes so the classifier sees the token.
@pytest.mark.parametrize(
    "command",
    [
        'git "push" --force',
        '"git" push --force',
        'git push origin "+main"',
    ],
)
def test_quoted_tokens_are_stripped_by_posix_shlex(command: str) -> None:
    assert decide_shell(command) == "hard_deny"


@pytest.mark.parametrize(
    "command",
    [
        "git show HEAD:.env",
        "git show HEAD:src/a.py",
        "git -c core.pager=cat status",
        "git diff /etc/passwd",
        "git diff ../outside.txt",
        "git branch newbranch",
        "git branch -m old new",
        "git log --exec-path",
        "git checkout main",
        "git commit -m msg",
        "git push origin main",
        "find . -exec rm {} ;",
        "node corpus/validation/validate.mjs",
    ],
)
def test_other_cases_need_an_approval(command: str) -> None:
    assert decide_shell(command) == "ask"


@pytest.mark.parametrize(
    "command", ["git diff -- .env", "git log -p -- deploy.pem", "git show .npmrc"]
)
def test_reading_a_secret_through_git_is_refused(command: str) -> None:
    assert decide_shell(command) == "hard_deny"


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git status --short --branch",
        "git log --oneline -5",
        "git log --since=2.days.ago --author=ana",
        "git diff --stat",
        "git diff HEAD~1 -- src/a.py",
        "git diff main..feature",
        "git show HEAD",
        "git branch --list",
        "git branch -a",
        "git branch --show-current",
    ],
)
def test_neg_ordinary_read_only_commands_stay_allowed(command: str) -> None:
    assert decide_shell(command) == "allow"


@pytest.mark.parametrize(
    "command", ["panda validate", "panda validate --root corpus/x", "panda validate --json"]
)
def test_panda_validate_through_the_shell_needs_an_approval(command: str) -> None:
    # Owner-approved change (R2-104): `--root` loads a nested workspace whose
    # validator the agent wrote. The `validate` tool is the unattended route.
    assert decide_shell(command) == "ask"


def test_neg_force_push_is_still_hard_denied_with_approval() -> None:
    from panda_core.errors import PolicyDenied

    with pytest.raises(PolicyDenied):
        engine().authorize("shell", "git push --force origin main", approved=True)


def test_the_classifier_applies_even_to_a_policy_that_allows_everything(
    workspace: Workspace,
) -> None:
    body = {
        "schema_version": 1,
        "default": "ask",
        "rules": [{"id": "all", "actions": ["*"], "resources": ["*"], "effect": "allow"}],
    }
    workspace.policy_path.write_text(json.dumps(body), encoding="utf-8")
    permissive = PolicyEngine(load_policy(workspace.policy_path), root=workspace.root)
    assert permissive.decide("shell", "git branch -D main").effect == "hard_deny"
    assert permissive.decide("shell", "git diff --output=x").effect == "deny"


def test_run_shell_does_not_overwrite_a_file_through_git_diff(workspace: Workspace) -> None:
    import subprocess

    root = workspace.root
    (root / "README.md").write_text("ORIGINAL\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    registry, _ = make_registry(workspace)
    result = call(registry, "run_shell", argv=["git", "diff", "--output=README.md"])
    assert result.blocked
    assert (root / "README.md").read_text(encoding="utf-8") == "ORIGINAL\n"


def test_run_shell_authorizes_by_argv_not_by_joined_text(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single argument that contains spaces must not hide a flag from the classifier."""

    registry, _ = make_registry(workspace)
    result = call(registry, "run_shell", argv=["git", "diff", "--output=README.md x"])
    assert result.blocked


# ------------------------------------------------------- SEC-002 (tool, MCP), MCP-001, MCP-002
import hashlib  # noqa: E402


def _policy_denying(workspace: Workspace, action: str, resource: str = "*") -> None:
    body = {
        "schema_version": 1,
        "default": "ask",
        "rules": [
            {"id": "ro", "actions": ["read"], "resources": ["*"], "effect": "allow"},
            {"id": f"no-{action}", "actions": [action], "resources": [resource], "effect": "deny"},
        ],
    }
    workspace.policy_path.write_text(json.dumps(body), encoding="utf-8")


def _marker_validator(workspace: Workspace, tmp_path) -> Path:  # noqa: F821
    marker = tmp_path / "validator-ran.txt"
    script = workspace.validator_path
    script.write_text(
        'import { writeFileSync } from "node:fs";\n'
        f"writeFileSync({json.dumps(str(marker))}, 'x');\n"
        'console.log(JSON.stringify({ok:true,checks:[]}));\n',
        encoding="utf-8",
    )
    return marker


def test_validate_tool_goes_through_the_policy(workspace: Workspace, tmp_path) -> None:
    marker = _marker_validator(workspace, tmp_path)
    _policy_denying(workspace, "validate")
    registry, _ = make_registry(workspace)
    result = call(registry, "validate")
    assert result.blocked
    assert not marker.exists()


def test_validate_tool_still_works_with_an_older_policy_file(workspace: Workspace) -> None:
    """A policy generated before the `validate` action existed must not break it."""

    body = {
        "schema_version": 1,
        "default": "ask",
        "rules": [{"id": "ro", "actions": ["read"], "resources": ["*"], "effect": "allow"}],
    }
    workspace.policy_path.write_text(json.dumps(body), encoding="utf-8")
    registry, _ = make_registry(workspace)
    result = call(registry, "validate")
    assert result.ok and not result.blocked


def test_validate_tool_records_the_validator_hash(workspace: Workspace) -> None:
    registry, _ = make_registry(workspace)
    result = call(registry, "validate")
    expected = hashlib.sha256(workspace.validator_path.read_bytes()).hexdigest()
    assert result.data["validator_sha256"] == expected


def test_shell_route_to_the_validator_needs_an_approval(workspace: Workspace, tmp_path) -> None:
    marker = _marker_validator(workspace, tmp_path)
    registry, _ = make_registry(workspace)
    result = call(registry, "run_shell", argv=["node", "corpus/validation/validate.mjs"])
    assert result.blocked
    assert not marker.exists()


def test_mcp_validate_goes_through_the_policy(workspace: Workspace, tmp_path) -> None:
    from panda_core.errors import PolicyDenied
    from panda_core.service import PandaService

    marker = _marker_validator(workspace, tmp_path)
    _policy_denying(workspace, "validate")
    with pytest.raises(PolicyDenied):
        PandaService(workspace).validate()
    assert not marker.exists()


def test_mcp_validate_returns_the_validator_hash(workspace: Workspace) -> None:
    from panda_core.service import PandaService

    payload = PandaService(workspace).validate()
    expected = hashlib.sha256(workspace.validator_path.read_bytes()).hexdigest()
    assert payload["validator_sha256"] == expected


def test_mcp_study_show_refuses_an_index_link_outside(workspace: Workspace, tmp_path) -> None:
    from panda_core.service import PandaService

    outside = _outside(tmp_path)
    _repoint_index(workspace, str(outside / "secret.md"))
    with pytest.raises(ConfigError):
        PandaService(workspace).study_show("gse160208")


def test_mcp_study_show_obeys_the_read_policy(workspace: Workspace) -> None:
    from panda_core.errors import PolicyDenied
    from panda_core.service import PandaService

    # The deny comes after the broad allow, and the last matching rule wins.
    _policy_denying(workspace, "read", "corpus/studies/gse160208.md")
    with pytest.raises(PolicyDenied):
        PandaService(workspace).study_show("gse160208")


def test_neg_mcp_study_show_and_validate_still_work(workspace: Workspace) -> None:
    from panda_core.service import PandaService

    service = PandaService(workspace)
    assert service.validate()["ok"] is True
    assert "Body text" in service.study_show("gse160208")["body"]


def test_mcp_every_exposed_read_goes_through_decide(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panda_core.service import PandaService

    seen: list[tuple[str, str]] = []
    real = PolicyEngine.decide

    def spy(self, action, resource):
        seen.append((action, resource))
        return real(self, action, resource)

    monkeypatch.setattr(PolicyEngine, "decide", spy)
    service = PandaService(workspace)
    service.validate()
    service.study_show("gse160208")
    actions = {action for action, _ in seen}
    assert {"validate", "read"} <= actions


@pytest.mark.parametrize("name", ["validate", "study_show", "panda_validate", "panda_study_show"])
def test_mcp_tool_rules_match_the_names_the_server_registers(name: str) -> None:
    assert engine().decide("tool", name).effect == "allow"


def test_neg_unknown_mcp_tool_still_asks() -> None:
    assert engine().decide("tool", "run_shell").effect == "ask"
