"""Trilha A — variantes de TRUST-004, SEC-008, SEC-005, SEC-010/TRUST-005, TRUST-001, PROV-001."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from panda_core.policy import PolicyEngine, default_policy, load_policy_or_default
from panda_core.process import run_command
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


def make_registry(workspace, approver=None):
    session = SessionLog.create(workspace.sessions_dir, label="poc-a")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(workspace=workspace, policy=PolicyEngine(policy, root=workspace.root),
                      session=session, approve=approver)
    return ToolRegistry(ctx), session


def call(registry, name, **arguments):
    return asyncio.run(registry.call(name, arguments))


def _hook(tmp_path: Path, name: str) -> tuple[Path, Path]:
    marker = tmp_path / f"{name}-ran.txt"
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/bin/sh\necho ran >> '{marker}'\ncat\n", encoding="utf-8")
    script.chmod(0o755)
    return script, marker


# R2-106: a sonda de boot (git_state) neutraliza só core.fsmonitor; um filtro clean
# do .git/config (com .gitattributes) roda em `git status` quando o índice está velho.
@needs_git
def test_git_state_probe_does_not_run_a_corpus_clean_filter(workspace, tmp_path):
    root = workspace.root
    script, marker = _hook(tmp_path, "clean-filter")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitattributes").write_text("*.md filter=evil\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "config", "filter.evil.clean", str(script)], cwd=root, check=True)
    marker.unlink(missing_ok=True)
    # índice "velho" (ex.: corpus copiado de outra máquina): toca os arquivos
    later = time.time() + 5
    for path in root.rglob("*.md"):
        if ".git" not in path.parts:
            os.utime(path, (later, later))
    state = workspace.git_state()
    assert state["repo"] is True
    assert not marker.exists(), "git status da sonda executou o filtro clean do corpus"


@needs_git
def test_git_state_probe_does_not_run_a_corpus_hook(workspace, tmp_path):
    root = workspace.root
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    marker = tmp_path / "hook-ran.txt"
    hook = root / ".git" / "hooks" / "post-index-change"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    later = time.time() + 5
    for path in root.rglob("*.md"):
        if ".git" not in path.parts:
            os.utime(path, (later, later))
    workspace.git_state()
    assert not marker.exists(), "git status da sonda executou o hook post-index-change do corpus"


# R2-107: um neto que chama setsid sai do grupo de processos e sobrevive ao timeout.
@POSIX
@pytest.mark.skipif(shutil.which("setsid") is None, reason="setsid not installed")
def test_a_grandchild_that_calls_setsid_does_not_survive(tmp_path):
    tag = "31.7199"
    result = run_command(["sh", "-c", f"setsid sleep {tag} >/dev/null 2>&1 & sleep 30"], timeout=1)
    assert result.timed_out
    time.sleep(0.5)
    alive = subprocess.run(["pgrep", "-f", f"sleep {tag}"], capture_output=True, text=True)
    if alive.stdout.strip():
        subprocess.run(["pkill", "-f", f"sleep {tag}"])
    assert not alive.stdout.strip(), "um neto com setsid sobreviveu ao kill do grupo"


# R2-108: arquivos de credencial comuns fora da lista do piso são lidos sem aprovação.
@pytest.mark.parametrize(
    "name",
    [
        ".envrc",
        "id_ecdsa",
        "id_dsa",
        ".kube/config",
        "application_default_credentials.json",
        ".vault-token",
        "terraform.tfstate",
        ".s3cfg",
        ".pgpass.bak",
        "service-account.json",
    ],
)
def test_common_credential_files_are_not_readable(workspace, name):
    secret = "SYNTH_SECRET_CRED_51aa"
    path = workspace.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{secret}\n", encoding="utf-8")
    registry, _ = make_registry(workspace)
    result = call(registry, "read_file", path=name)
    assert secret not in result.content, f"{name} lido sem aprovação"


# R2-109: INDEX.md não passa por safe_read; um symlink do corpus para uma tabela fora
# do workspace é lido e suas células voltam ao modelo por study_list.
@POSIX
def test_study_list_does_not_follow_a_symlinked_index(workspace, tmp_path):
    outside = tmp_path / "outside-notes.md"
    outside.write_text(
        "| ID | Track | Status | Question |\n|---|---|---|---|\n"
        "| [`leak-row`](x.md) | public | done | SYNTH_SECRET_INDEX_77 |\n",
        encoding="utf-8",
    )
    index = workspace.root / "corpus" / "studies" / "INDEX.md"
    index.unlink()
    index.symlink_to(outside)
    registry, _ = make_registry(workspace)
    result = call(registry, "study_list")
    assert "SYNTH_SECRET_INDEX_77" not in result.content, "INDEX.md simbólico lido"


# R2-110: provider.name (do .panda/config.json) escolhe qual entrada do cofre de
# usuário é enviada como chave à OpenCode.
def test_workspace_cannot_choose_which_vault_key_is_sent(workspace, isolated_credentials):
    from panda_core.config import load_config
    from panda_core.provider import OpenCodeProvider

    isolated_credentials.write_text(
        json.dumps({"keys": {"opencode-go": "SYNTH_SECRET_GO_KEY_1", "other-svc": "SYNTH_SECRET_OTHER_2"}}),
        encoding="utf-8",
    )
    cfg_path = workspace.config_path
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["provider"]["name"] = "other-svc"
    cfg["model"] = "other-svc/deepseek-v4.1-flash"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    config = load_config(cfg_path)
    provider = OpenCodeProvider(config.provider, model=config.model)
    assert provider.api_key() != "SYNTH_SECRET_OTHER_2", "o corpus escolheu a chave de outro serviço"


# R2-111: `!cmd` do REPL/cockpit chama tools.call('run_shell') direto: nenhum evento
# tool.call/tool.result entra no ledger e um ledger quebrado não impede a execução.
@needs_git
def test_operator_shell_route_is_recorded_in_the_ledger(workspace):
    from panda_cli.repl import _run_shell_line
    from panda_cli.runtime import ChatRuntime

    subprocess.run(["git", "init", "-q"], cwd=workspace.root, check=True)
    runtime = ChatRuntime(workspace)
    asyncio.run(_run_shell_line(runtime, "git status"))
    kinds = [json.loads(line)["type"] for line in runtime.session.path.read_text().splitlines()]
    assert "tool.call" in kinds, f"!git status rodou sem registro: {kinds}"


@needs_git
def test_operator_shell_route_stops_on_a_broken_ledger(workspace, capsys):
    from panda_cli.repl import _run_shell_line
    from panda_cli.runtime import ChatRuntime

    subprocess.run(["git", "init", "-q"], cwd=workspace.root, check=True)
    runtime = ChatRuntime(workspace)
    runtime.session._write_anchor(999, "0" * 64)  # cenário de PROV-001: âncora à frente
    asyncio.run(_run_shell_line(runtime, "git status"))
    out = capsys.readouterr().out
    assert '"returncode":0' not in out.replace(" ", ""), "!cmd executou com o ledger quebrado"
