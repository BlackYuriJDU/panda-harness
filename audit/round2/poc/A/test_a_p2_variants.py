"""Trilha A — variantes de TRUST-003, VAL-002, AGT-003, POL-001, APR-002/C-4, SEC-011,
VAL-001, MCP-002, EDIT-001, PROV-004 (rodada 2, 704f387)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from panda_core.policy import PolicyEngine, load_policy, load_policy_or_default
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


def make_registry(workspace, approver=None):
    session = SessionLog.create(workspace.sessions_dir, label="poc-a")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(workspace=workspace, policy=PolicyEngine(policy, root=workspace.root),
                      session=session, approve=approver)
    return ToolRegistry(ctx), session


def call(registry, name, **arguments):
    return asyncio.run(registry.call(name, arguments))


def write_policy(workspace, rules, default="ask"):
    workspace.policy_path.write_text(
        json.dumps({"schema_version": 1, "default": default, "rules": rules}), encoding="utf-8"
    )


# R2-113: CONTRACT §8 diz "A workspace policy can only tighten the result"; uma regra
# 'allow' do policy.json do corpus abre shell arbitrário e escrita fora dos escopos.
def test_a_workspace_policy_cannot_loosen_shell_or_writes(workspace, tmp_path):
    write_policy(workspace, [
        {"id": "r", "actions": ["read"], "resources": ["*"], "effect": "allow"},
        {"id": "x", "actions": ["shell", "write"], "resources": ["*"], "effect": "allow"},
    ])
    registry, _ = make_registry(workspace, approver=None)
    marker = tmp_path / "loosened.txt"
    shell = call(registry, "run_shell", argv=[sys.executable, "-c",
                                             f"open({str(marker)!r},'w').write('x')"])
    hook = call(registry, "write_file", path=".git/hooks/pre-commit", content="#!/bin/sh\n")
    assert shell.blocked and hook.blocked, (
        f"policy do workspace afrouxou: shell.blocked={shell.blocked} write.blocked={hook.blocked}"
    )


# R2-114: o ambiente do validador/run_shell só remove nomes com sufixo _KEY/_TOKEN/...;
# PGPASSWORD, MYSQL_PWD, DATABASE_URL com senha e SSH_AUTH_SOCK passam.
@POSIX
def test_child_env_drops_common_secret_variables(monkeypatch):
    from panda_core.process import scrubbed_env

    monkeypatch.setenv("PGPASSWORD", "SYNTH_SECRET_PG")
    monkeypatch.setenv("MYSQL_PWD", "SYNTH_SECRET_MY")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:SYNTH_SECRET_DB@h/db")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/SYNTH-agent.sock")
    env = scrubbed_env()
    leaked = [n for n in ("PGPASSWORD", "MYSQL_PWD", "DATABASE_URL", "SSH_AUTH_SOCK") if n in env]
    assert not leaked, f"herdadas pelo filho: {leaked}"


# R2-115: motivos de parada que o agente não reconhece como incompletos executam as
# tool calls (Anthropic 'model_context_window_exceeded', Google 'UNEXPECTED_TOOL_CALL').
@pytest.mark.parametrize("reason", ["model_context_window_exceeded", "unexpected_tool_call",
                                    "too_many_tool_calls", "malformed_response"])
def test_unknown_finish_reasons_do_not_run_tool_calls(workspace, reason):
    from panda_core.agent import Agent
    from panda_core.provider import DoneEvent, ToolCall

    class Script:
        def __init__(self):
            self.n = 0

        async def stream(self, messages, tools=None, model=None, session_id=None):
            self.n += 1
            if self.n == 1:
                c = ToolCall(id="c1", name="write_file",
                             arguments=json.dumps({"path": "corpus/cut.md", "content": "x"}))
                yield DoneEvent(finish_reason=reason, tool_calls=(c,))
            else:
                yield DoneEvent(finish_reason="stop")

    registry, session = make_registry(workspace)
    agent = Agent(workspace, Script(), registry, session=session, max_steps=3)
    result = asyncio.run(agent.run_turn("x"))
    assert not (workspace.root / "corpus" / "cut.md").exists(), (
        f"finish_reason={reason!r} tratado como completo (status={result.status})"
    )


# R2-116: 'actions'/'resources' como string (não lista) é aceito e vira tupla de letras;
# uma regra deny escrita assim deixa de valer em silêncio (irmão de POL-001).
def test_policy_rule_with_string_lists_is_refused(workspace):
    from panda_core.errors import ConfigError

    write_policy(workspace, [
        {"id": "r", "actions": ["read"], "resources": ["*"], "effect": "allow"},
        {"id": "no-readme", "actions": "read", "resources": "README.md", "effect": "deny"},
    ])
    with pytest.raises(ConfigError):
        load_policy(workspace.policy_path)


def test_policy_rule_with_unknown_risk_is_refused(workspace):
    from panda_core.errors import ConfigError

    write_policy(workspace, [
        {"id": "x", "actions": ["shell"], "resources": ["*"], "effect": "ask", "risk": "trivial"},
    ])
    with pytest.raises(ConfigError):
        load_policy(workspace.policy_path)


# R2-117: a prévia de aprovação do run_shell é " ".join(argv): argv distintos geram a
# mesma prévia (o operador não vê o que roda — C-4).
def test_run_shell_preview_distinguishes_argument_boundaries(workspace):
    seen = []

    async def recorder(request):
        seen.append(request.preview)
        return False

    registry, _ = make_registry(workspace, approver=recorder)
    call(registry, "run_shell", argv=["rm", "notes.md", "-i"])
    call(registry, "run_shell", argv=["rm", "notes.md -i"])
    assert seen[0] != seen[1], f"prévias idênticas para argv diferentes: {seen}"


# SEC-011 (refutação esperada): diretório corpus/claims simbólico para fora.
@POSIX
def test_claims_directory_symlink_is_not_followed(workspace, tmp_path):
    outside = tmp_path / "outside-claims"
    outside.mkdir()
    (outside / "c1.md").write_text("---\nclaim_id: c1\n---\nSYNTH_SECRET_CLAIMDIR\n", "utf-8")
    (workspace.root / "corpus" / "claims").symlink_to(outside, target_is_directory=True)
    registry, _ = make_registry(workspace)
    result = call(registry, "claim_show", claim_id="c1")
    assert "SYNTH_SECRET_CLAIMDIR" not in result.content


# VAL-001 (variantes): nomes duplicados e checagem "ok" mas "skipped".
def test_duplicate_check_names_cannot_hide_a_skip():
    from panda_core.errors import ProtocolError
    from panda_core.validator import CHECK_NAMES, parse_report

    checks = [{"name": n, "ok": True, "skipped": False} for n in CHECK_NAMES]
    checks.append({"name": "schema", "ok": True, "skipped": True})  # duplicata pulada vence
    with pytest.raises(ProtocolError):
        parse_report(json.dumps({"ok": True, "checks": checks}))


def test_trailing_garbage_json_is_not_the_report():
    from panda_core.errors import ProtocolError
    from panda_core.validator import CHECK_NAMES, parse_report

    good = {"ok": True, "checks": [{"name": n, "ok": True} for n in CHECK_NAMES]}
    text = json.dumps(good) + "\n" + '{"note": "x"}'
    with pytest.raises(ProtocolError):
        parse_report(text)


# MCP-002 (variante): study_list ignora a política de leitura (INDEX.md) no MCP e no agente.
def test_study_list_obeys_a_read_deny_on_the_registry(workspace):
    write_policy(workspace, [
        {"id": "r", "actions": ["read"], "resources": ["*"], "effect": "allow"},
        {"id": "no-studies", "actions": ["read"], "resources": ["corpus/studies/*"],
         "effect": "deny"},
    ])
    registry, _ = make_registry(workspace)
    shown = call(registry, "study_show", study_id="gse160208")
    listed = call(registry, "study_list")
    assert shown.blocked
    assert listed.blocked or "Programas?" not in listed.content, "study_list leu INDEX negado"


# EDIT-001 (variante): new_string com CR solto cria finais mistos em silêncio.
def test_edit_does_not_introduce_mixed_line_endings(workspace):
    path = workspace.root / "corpus" / "lf.md"
    path.write_bytes(b"a\nb\nc\n")
    registry, _ = make_registry(workspace)
    call(registry, "edit_file", path="corpus/lf.md", old_string="b", new_string="b1\rb2")
    data = path.read_bytes()
    assert b"\r" not in data or b"\n" not in data.replace(b"\r\n", b""), data
