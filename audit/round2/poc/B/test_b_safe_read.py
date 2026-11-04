"""Trilha B — safe_read.py. Afirma o resultado SEGURO."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import panda_core.safe_read as safe_read
from panda_core.errors import ConfigError


@pytest.mark.skipif(sys.platform == "win32", reason="symlink POSIX")
def test_parent_directory_swapped_after_lstat_walk_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O docstring promete a 2a camada contra troca após a checagem; O_NOFOLLOW só vale
    para o último componente, então trocar um diretório pai por link ainda é seguido."""

    root = tmp_path / "ws"
    (root / ".panda").mkdir(parents=True)
    (root / ".panda" / "instructions.md").write_text("benign\n", encoding="utf-8")
    outside = tmp_path / "home" / "secrets"
    outside.mkdir(parents=True)
    (outside / "instructions.md").write_text("SYNTH_SECRET_OUTSIDE\n", encoding="utf-8")

    real_check = safe_read.ensure_plain_path

    def check_then_swap(r: Path, p: Path) -> Path:
        result = real_check(r, p)  # a checagem passa com diretórios comuns
        (root / ".panda").rename(root / ".panda.bak")  # janela de corrida simulada
        os.symlink(outside, root / ".panda")
        return result

    monkeypatch.setattr(safe_read, "ensure_plain_path", check_then_swap)
    try:
        text = safe_read.read_workspace_file(root, root / ".panda" / "instructions.md")
    except (ConfigError, OSError):
        return
    assert "SYNTH_SECRET" not in text


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node ausente")
def test_validator_bare_import_cannot_resolve_to_agent_writable_node_modules(
    workspace, tmp_path: Path  # type: ignore[no-untyped-def]
) -> None:
    """Se o validador (protegido) importa um pacote, a resolução do Node sobe até
    corpus/node_modules, que o agente pode escrever (corpus/* é allow)."""

    import asyncio
    import json

    from panda_core.policy import PolicyEngine, load_policy_or_default
    from panda_core.tools import ToolContext, ToolRegistry

    script = workspace.root / "corpus" / "validation" / "validate.mjs"
    original = script.read_text(encoding="utf-8")
    script.write_text("import 'corpus-helpers';\n" + original, encoding="utf-8")  # o mantenedor
    policy, _ = load_policy_or_default(workspace.policy_path)
    registry = ToolRegistry(ToolContext(workspace=workspace, policy=PolicyEngine(policy)))
    marker = tmp_path / "PWNED_BY_NODE_MODULES"
    pkg = {"name": "corpus-helpers", "version": "1.0.0", "type": "module", "main": "index.js"}
    for path, content in {
        "corpus/node_modules/corpus-helpers/package.json": json.dumps(pkg),
        "corpus/node_modules/corpus-helpers/index.js":
            f"import {{ writeFileSync }} from 'node:fs'; writeFileSync({str(marker)!r}, 'x');\n",
    }.items():
        assert asyncio.run(registry.call("write_file", {"path": path, "content": content})).ok
    asyncio.run(registry.call("validate", {}))
    assert not marker.exists(), "validate (allow) executou código de corpus/node_modules"
