"""Fixtures: a small corpus with a real Node validator and an initialized workspace."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from panda_core.init import initialize_workspace
from panda_core.workspace import Workspace

FAKE_VALIDATOR = r"""
const fail = process.env.PANDA_FAKE_FAIL || "";
const sleep = Number(process.env.PANDA_FAKE_SLEEP || "0");
const release = process.argv.includes("--release");
const names = [
  "schema", "study", "track", "evidence", "numbers",
  "claims", "findings", "license", "public-face", "secrets",
];
const checks = names.map((name) => ({
  name,
  ok: name !== fail,
  skipped: false,
  reason: "",
  errors: name === fail ? ["forced failure in " + name] : [],
  warnings: [],
}));
const ok = checks.every((check) => check.ok);
const report = {
  ok,
  generated_at: new Date().toISOString(),
  root: process.cwd(),
  release_mode: release,
  checks,
};
const emit = () => {
  console.log(JSON.stringify(report, null, 2));
  process.exit(ok ? 0 : 1);
};
if (sleep > 0) {
  setTimeout(emit, sleep);
} else {
  emit();
}
"""

INDEX = """# Registry de estudos — fixture

| ID | Trilha | Status | Pergunta | Dados | Licença | Próximo passo |
|---|---|---|---|---|---|---|
| [`gse160208`](gse160208.md) | public | done | Programas? | GEO | GEO / pública | Camada |
| [`caso-referencia`](caso-referencia.md) | simulated | active | Caso? | local | projeto | Uso |
"""

CARD = """---
study_id: gse160208
track: public
status: done
license: GEO / public
---

# GSE160208

Body text with a citation at `corpus/studies/gse160208.md:12`.
"""


@pytest.fixture()
def fake_corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus-root"
    validator = root / "corpus" / "validation" / "validate.mjs"
    validator.parent.mkdir(parents=True)
    validator.write_text(FAKE_VALIDATOR, encoding="utf-8", newline="\n")

    studies = root / "corpus" / "studies"
    studies.mkdir(parents=True)
    (studies / "INDEX.md").write_text(INDEX, encoding="utf-8", newline="\n")
    (studies / "gse160208.md").write_text(CARD, encoding="utf-8", newline="\n")
    (studies / "caso-referencia.md").write_text(
        "---\nstudy_id: caso-referencia\ntrack: simulated\n---\n\nSimulated case.\n",
        encoding="utf-8",
        newline="\n",
    )

    (root / "corpus" / "evidence.md").write_text("# evidence\n", encoding="utf-8")
    memory = root / "memory"
    memory.mkdir()
    (memory / ".gitkeep").write_text("", encoding="utf-8")
    (root / "README.md").write_text("# fixture corpus\n", encoding="utf-8")
    return root


@pytest.fixture()
def workspace(fake_corpus: Path) -> Workspace:
    initialize_workspace(fake_corpus)
    return Workspace.from_root(fake_corpus)


@pytest.fixture()
def wired_workspace(fake_corpus: Path) -> Workspace:
    initialize_workspace(
        fake_corpus,
        opencode=True,
        panda_command=[sys.executable, "-m", "panda_cli"],
    )
    return Workspace.from_root(fake_corpus)
