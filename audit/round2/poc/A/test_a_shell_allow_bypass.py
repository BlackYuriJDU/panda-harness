"""Trilha A — variantes de SEC-001/SEC-007/TRUST-004 (rodada 2, 704f387).

Cada teste afirma o resultado SEGURO; falha hoje quando o achado é real.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from panda_core.policy import PolicyEngine, default_policy, load_policy_or_default
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git not installed")


def make_registry(workspace, approver=None):
    session = SessionLog.create(workspace.sessions_dir, label="poc-a")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
        approve=approver,
    )
    return ToolRegistry(ctx)


def call(registry, name, **arguments):
    return asyncio.run(registry.call(name, arguments))


# R2-100: argv[0] que começa com o texto de um prefixo "allow" ("git status") e
# percorre um diretório chamado "git status" executa qualquer binário sem aprovação.
def test_run_shell_argv0_with_allowed_prefix_cannot_run_another_program(workspace, tmp_path):
    registry = make_registry(workspace, approver=None)  # sem aprovador: 'ask' => bloqueado
    created = call(registry, "write_file", path="corpus/git status/x.md", content="x\n")
    assert created.ok, created.content
    cwd = workspace.root / "corpus"
    target = shutil.which("touch")
    assert target
    ups = "../" * (len(cwd.resolve().parts) + 1)  # sobe até "/" a partir de "corpus/git status"
    argv0 = "git status/" + ups + target.lstrip("/")
    marker = tmp_path / "SYNTH_marker_argv0.txt"
    result = call(registry, "run_shell", argv=[argv0, str(marker)], cwd="corpus")
    assert result.blocked, f"executou sem aprovação: {result.content[:300]}"
    assert not marker.exists()


def test_decide_shell_joined_text_prefix_is_not_the_program():
    engine = PolicyEngine(default_policy())
    decision = engine.decide_shell(["git status/../../usr/bin/python3", "-c", "print(1)"])
    assert decision.effect != "allow", decision


# R2-101: core.fsmonitor de .git/config (corpus hostil) roda via run_shell `git status`,
# que é 'allow' sem aprovação; o fix de TRUST-004 cobre só a sonda de boot.
@needs_git
def test_run_shell_git_status_does_not_run_a_corpus_fsmonitor(workspace, tmp_path):
    marker = tmp_path / "fsmonitor-ran.txt"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=workspace.root, check=True)
    subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=workspace.root, check=True)
    registry = make_registry(workspace, approver=None)
    result = call(registry, "run_shell", argv=["git", "status"])
    assert not marker.exists(), f"fsmonitor do corpus executou (blocked={result.blocked})"


@needs_git
def test_run_shell_git_diff_does_not_run_a_corpus_external_diff(workspace, tmp_path):
    marker = tmp_path / "extdiff-ran.txt"
    hook = tmp_path / "extdiff.sh"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    root = workspace.root
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"],
                   cwd=root, check=True)
    (root / "README.md").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "config", "diff.external", str(hook)], cwd=root, check=True)
    registry = make_registry(workspace, approver=None)
    result = call(registry, "run_shell", argv=["git", "diff"])
    assert not marker.exists(), f"diff.external do corpus executou (blocked={result.blocked})"


@needs_git
def test_run_shell_git_show_does_not_run_a_corpus_textconv(workspace, tmp_path):
    marker = tmp_path / "textconv-ran.txt"
    hook = tmp_path / "textconv.sh"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\ncat \"$1\"\n", encoding="utf-8")
    hook.chmod(0o755)
    root = workspace.root
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    # .gitattributes dentro de corpus/ (escopo gravável pelo agente) + driver no .git/config
    (root / "corpus" / ".gitattributes").write_text("*.md diff=tc\n", encoding="utf-8")
    subprocess.run(["git", "config", "diff.tc.textconv", str(hook)], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"],
                   cwd=root, check=True)
    registry = make_registry(workspace, approver=None)
    result = call(registry, "run_shell", argv=["git", "show", "HEAD"])
    assert result.ok or result.blocked
    assert not marker.exists(), f"textconv do corpus executou (blocked={result.blocked})"


# R2-102: segredos rastreados saem por `git diff`/`git show`/`git log -p` sem caminho
# ou com pathspec glob, todos 'allow' sem aprovação (read-secrets é contornado).
SYNTH = "SYNTH_SECRET_GITDIFF_4d2a"


@pytest.fixture()
def repo_with_env(workspace):
    root = workspace.root
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".env").write_text("TOKEN=placeholder\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"],
                   cwd=root, check=True)
    (root / ".env").write_text(f"TOKEN={SYNTH}\n", encoding="utf-8")
    return workspace


@needs_git
@pytest.mark.parametrize(
    "argv",
    [["git", "diff"], ["git", "diff", "--", ".e*"], ["git", "diff", "--", "[.]env"]],
)
def test_git_read_only_cannot_print_a_hard_denied_file(repo_with_env, argv):
    registry = make_registry(repo_with_env, approver=None)
    result = call(registry, "run_shell", argv=argv)
    assert SYNTH not in result.content, f"{argv} vazou .env sem aprovação"


@needs_git
def test_git_show_head_cannot_print_a_committed_secret(workspace):
    root = workspace.root
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".env").write_text(f"TOKEN={SYNTH}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "x"],
                   cwd=root, check=True)
    registry = make_registry(workspace, approver=None)
    for argv in (["git", "show", "HEAD"], ["git", "log", "-p"]):
        result = call(registry, "run_shell", argv=argv)
        assert SYNTH not in result.content, f"{argv} vazou .env commitado sem aprovação"


# R2-103: hard_deny de shell rebaixado a 'ask' (e, com --auto, aprovado) por formas que
# o classificador não reconhece.
@pytest.mark.parametrize(
    "command",
    [
        "rm --r corpus",                      # abreviação GNU única de --recursive
        "rm --rec corpus",
        "timeout 60 git push --force origin main",   # wrapper não desembrulhado
        "stdbuf -o0 git reset --hard",
        "sudo -u root git push --force origin main",  # opção com valor do wrapper
        "env -u HOME git push --force origin main",
        "env -C . git reset --hard",
        "nice -n 5 rm -rf corpus",               # (controle: deve ser hard_deny)
        "git push --prune origin refs/heads/*:refs/heads/*",  # apaga branches remotas
    ],
)
def test_destructive_forms_stay_hard_denied(command):
    engine = PolicyEngine(default_policy())
    assert engine.decide("shell", command).effect == "hard_deny"
