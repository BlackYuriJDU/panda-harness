"""Trilha B — shell_policy.py / policy.decide_shell. Cada teste afirma o resultado SEGURO."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from panda_core.policy import PolicyEngine, default_policy
from panda_core.tools import ToolContext, ToolRegistry
from panda_core.workspace import Workspace

GIT = shutil.which("git")


def _engine() -> PolicyEngine:
    return PolicyEngine(default_policy())


# ---------- R2-202: hard_deny rebaixado para ask por grafias irmãs ----------
@pytest.mark.parametrize(
    "argv",
    [
        ["git", "--no-replace-objects", "push", "--force", "origin", "main"],
        ["git", "--literal-pathspecs", "reset", "--hard"],
        ["git", "--glob-pathspecs", "clean", "-fdx"],
        ["env", "-u", "HOME", "git", "push", "--force"],
        ["env", "-C", ".", "git", "push", "-f"],
        ["sudo", "-u", "me", "git", "reset", "--hard"],
        ["timeout", "60", "git", "push", "--force"],
        ["stdbuf", "-o0", "git", "push", "--force"],
        ["setsid", "git", "push", "--force"],
        ["busybox", "rm", "-rf", "corpus"],
        ["git", "-c", "remote.origin.mirror=true", "push", "origin"],
        ["git", "push", "--prune", "origin", "refs/heads/*:refs/heads/*"],
    ],
)
def test_force_and_destructive_spellings_stay_hard_denied(argv: list[str]) -> None:
    decision = _engine().decide_shell(argv)
    assert decision.effect == "hard_deny", (argv, decision.effect, decision.rule_id)


# ---------- R2-203: paridade texto exibido/regras (" ".join(argv)) x argv executado ----------
@pytest.mark.parametrize(
    "argv",
    [
        ["env", "-S", "git push --force"],
        ["git", "push", "origin main --force"],
    ],
)
def test_text_shown_in_preview_decides_like_the_argv_that_runs(argv: list[str]) -> None:
    # run_shell monta o preview e o texto das regras com " ".join(argv) (tools.py:834);
    # policy_check/MCP decide esse mesmo texto via shlex.split (policy.py:436).
    engine = _engine()
    by_argv = engine.decide_shell(argv).effect
    by_text = engine.decide("shell", " ".join(argv)).effect
    assert by_argv == by_text, (argv, by_argv, by_text)


def test_windows_string_tokenization_keeps_quoted_force_hard_denied() -> None:
    # decide() usa shlex.split(posix=False) quando os.name == "nt" (policy.py:436).
    text = 'git -C . push "--force" origin main'
    tokens = shlex.split(text, posix=False)
    assert _engine().decide_shell(tokens, text=text).effect == "hard_deny", tokens


# ---------- M26 (sobreviveu): `git clean -f` só é hard_deny pela regra de texto ----------
@pytest.mark.parametrize(
    "argv",
    [["git", "-C", ".", "clean", "-f"], ["git", "--no-pager", "clean", "-fdx"],
     ["git", "-c", "x.y=z", "clean", "--force"]],
)
def test_git_clean_force_with_global_option_is_hard_denied(argv: list[str]) -> None:
    assert _engine().decide_shell(argv).effect == "hard_deny"


# ---------- R2-200 / R2-201: comandos "read-only" de git sem aprovação ----------
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd), "PATH": "/usr/bin:/bin",
             "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": "a",
             "GIT_COMMITTER_EMAIL": "a@b"},
    )


def _registry(workspace: Workspace) -> ToolRegistry:
    from panda_core.policy import load_policy_or_default

    policy, _ = load_policy_or_default(workspace.policy_path)
    # approve=None: qualquer `ask` vira bloqueio; só `allow` executa.
    return ToolRegistry(ToolContext(workspace=workspace, policy=PolicyEngine(policy), approve=None))


@pytest.mark.skipif(GIT is None, reason="git ausente")
def test_git_log_patch_does_not_print_committed_secret_file(workspace: Workspace) -> None:
    root = workspace.root
    _git(root, "init", "-q")
    (root / ".env").write_text("DBX=SYNTH_SECRET_ENV_VALUE_123\n", encoding="utf-8")
    (root / "id_ed25519").write_text("SYNTH_SECRET_KEY_MATERIAL\n", encoding="utf-8")
    _git(root, "add", "-f", ".env", "id_ed25519")
    _git(root, "commit", "-qm", "x")
    registry = _registry(workspace)
    direct = asyncio.run(registry.call("read_file", {"path": ".env"}))
    assert direct.blocked  # a leitura direta é hard_deny
    for argv in (["git", "log", "-p"], ["git", "show", "HEAD"]):
        result = asyncio.run(registry.call("run_shell", {"argv": argv}))
        assert "SYNTH_SECRET" not in result.content, (argv, result.content[:300])


@pytest.mark.skipif(GIT is None, reason="git ausente")
def test_git_status_in_agent_writable_nested_repo_runs_no_config_hook(
    workspace: Workspace, tmp_path: Path
) -> None:
    root = workspace.root
    marker = tmp_path / "PWNED_BY_FSMONITOR"
    registry = _registry(workspace)
    # Tudo abaixo é feito com write_file (corpus/* é `allow` na política padrão).
    files = {
        "corpus/sub/.git/HEAD": "ref: refs/heads/main\n",
        "corpus/sub/.git/objects/keep": "x",
        "corpus/sub/.git/refs/heads/keep": "x",
        "corpus/sub/.git/config": (
            "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
            f"\tfsmonitor = touch {marker}; false\n"
        ),
    }
    for path, content in files.items():
        written = asyncio.run(registry.call("write_file", {"path": path, "content": content}))
        assert written.ok, written.content
    result = asyncio.run(
        registry.call("run_shell", {"argv": ["git", "status"], "cwd": "corpus/sub"})
    )
    assert not marker.exists(), ("core.fsmonitor ran without approval", result.content[:300])


# ---------- R2-204: outras formas de publicar são `ask` comum -> aprovadas por --auto ----------
@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "release", "create", "v1", "dist/x.whl"],
        ["twine", "upload", "dist/*"],
        ["npm", "publish"],
        ["git", "send-email", "--to=x@example.org", "0001.patch"],
        ["gh", "pr", "merge", "1", "--admin"],
        ["git", "remote", "set-url", "--push", "origin", "https://example.org/x.git"],
    ],
)
def test_auto_mode_does_not_approve_other_publishing_commands(argv: list[str]) -> None:
    from types import SimpleNamespace

    from panda_cli.runtime import ChatRuntime
    from panda_core.tools import ApprovalRequest

    decision = _engine().decide_shell(argv)
    request = ApprovalRequest(
        tool="run_shell", action="shell", resource=" ".join(argv), rule_id=decision.rule_id,
        risk=decision.risk, reason=decision.reason, preview=" ".join(argv), preview_kind="command",
    )
    fake = SimpleNamespace(auto=True, _approver=None)
    approved = asyncio.run(ChatRuntime._approve(fake, request))  # type: ignore[arg-type]
    assert decision.effect != "allow"
    assert approved is False, (argv, decision.rule_id)


# ---------- R2-205: `panda validate*` é allow por texto e aceita --root/cwd escolhidos pelo agente ----------
@pytest.mark.skipif(shutil.which("node") is None, reason="node ausente")
def test_panda_validate_via_run_shell_cannot_run_agent_written_validator(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import sys

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    marker = tmp_path / "PWNED_BY_NESTED_VALIDATOR"
    registry = _registry(workspace)
    js = (
        "import { writeFileSync } from 'node:fs';\n"
        "writeFileSync(" + repr(str(marker)) + ", 'x');\n"
        "console.log(JSON.stringify({ok:true,checks:[{name:'schema',ok:true,skipped:false,"
        "reason:'',errors:[],warnings:[]}]}));\n"
    )
    written = asyncio.run(registry.call(
        "write_file", {"path": "corpus/evil/corpus/validation/validate.mjs", "content": js}
    ))
    assert written.ok, written.content  # corpus/* é allow; o floor só cobre corpus/validation/*
    config = (workspace.root / ".panda" / "config.json").read_text(encoding="utf-8")
    written = asyncio.run(registry.call(
        "write_file", {"path": "corpus/evil/.panda/config.json", "content": config}
    ))
    assert written.ok, written.content  # o floor cobre `.panda/*` só na raiz
    result = asyncio.run(registry.call(
        "run_shell", {"argv": ["panda", "validate", "--root", "corpus/evil"]}
    ))
    assert not marker.exists(), ("validator escrito pelo agente rodou sem aprovação",
                                 result.content[:400])
