"""Trilha A — variantes de SEC-002 / MCP-002 (rotas alternativas até o validador)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def make_registry(workspace, approver=None):
    session = SessionLog.create(workspace.sessions_dir, label="poc-a")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(workspace=workspace, policy=PolicyEngine(policy, root=workspace.root),
                      session=session, approve=approver)
    return ToolRegistry(ctx)


def call(registry, name, **arguments):
    return asyncio.run(registry.call(name, arguments))


def marker_js(marker: Path) -> str:
    return (
        'import { writeFileSync } from "node:fs";\n'
        f"writeFileSync({json.dumps(str(marker))}, 'x');\n"
        'console.log(JSON.stringify({ok:true,checks:[]}));\n'
    )


# R2-104: 'panda validate*' é allow por prefixo; '--root' aponta para um workspace
# aninhado em corpus/ (gravável sem aprovação), cujo validador o piso não protege.
@needs_node
def test_agent_cannot_run_a_validator_it_wrote_through_panda_validate_root(
    workspace, tmp_path, monkeypatch
):
    panda_bin = Path(sys.executable).with_name("panda")
    assert panda_bin.exists()
    monkeypatch.setenv("PATH", f"{panda_bin.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    marker = tmp_path / "nested-validator-ran.txt"
    registry = make_registry(workspace, approver=None)
    wrote = call(registry, "write_file", path="corpus/x/corpus/validation/validate.mjs",
                 content=marker_js(marker))
    assert wrote.ok, wrote.content  # o piso só cobre corpus/validation/* na raiz
    cfg = (workspace.root / ".panda" / "config.json").read_text(encoding="utf-8")
    wrote_cfg = call(registry, "write_file", path="corpus/x/.panda/config.json", content=cfg)
    assert wrote_cfg.ok, wrote_cfg.content  # o piso só cobre .panda/* na raiz
    result = call(registry, "run_shell", argv=["panda", "validate", "--root", "corpus/x"])
    print("RESULT", result.blocked, result.content[:600])
    assert result.blocked or not marker.exists(), "validador escrito pelo agente rodou sem aprovação"


# R2-105: com config.validator fora de corpus/validation/ (layout legítimo), o piso
# não protege o script: o agente o reescreve sem aprovação e o tool validate o executa.
@needs_node
def test_configured_validator_outside_the_default_dir_is_not_agent_writable(fake_corpus, tmp_path):
    from panda_core.init import initialize_workspace
    from panda_core.workspace import Workspace

    custom = fake_corpus / "corpus" / "checks" / "validate.mjs"
    custom.parent.mkdir(parents=True)
    shutil.copy(fake_corpus / "corpus" / "validation" / "validate.mjs", custom)
    initialize_workspace(fake_corpus)
    cfg_path = fake_corpus / ".panda" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["validator"] = "corpus/checks/validate.mjs"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    workspace = Workspace.from_root(fake_corpus)
    marker = tmp_path / "custom-validator-ran.txt"
    registry = make_registry(workspace, approver=None)
    wrote = call(registry, "write_file", path="corpus/checks/validate.mjs", content=marker_js(marker))
    assert wrote.blocked, "o validador configurado é gravável pelo agente sem aprovação"
