"""Trilha B — tests/test_workflows.py como especificação: mutações perigosas devem ser pegas."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

SRC = Path("/home/user/panda-audit-src")


def _load_spec_module():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("wf_spec", SRC / "tests" / "test_workflows.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _suite_catches(tmp_path: Path, mutate) -> bool:  # type: ignore[no-untyped-def]
    github = tmp_path / ".github"
    shutil.copytree(SRC / ".github", github)
    release = github / "workflows" / "release.yml"
    release.write_text(mutate(release.read_text(encoding="utf-8")), encoding="utf-8")
    module = _load_spec_module()
    module.GITHUB = github
    failures = 0
    for name in dir(module):
        if name.startswith("test_"):
            try:
                getattr(module, name)()
            except AssertionError:
                failures += 1
    return failures > 0


MUTATIONS = {
    "job-level write-all in the test job": lambda t: t.replace(
        "    name: Test before release\n    runs-on: ubuntu-latest\n",
        "    name: Test before release\n    runs-on: ubuntu-latest\n    permissions: write-all\n"),
    "id-token write in the build job": lambda t: t.replace(
        "    needs: test\n    steps:",
        "    needs: test\n    permissions:\n      contents: read\n      id-token: write\n    steps:"),
    "pull_request_target trigger": lambda t: t.replace(
        "  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request_target:\n"),
    "tests skipped with continue-on-error": lambda t: t.replace(
        "      - name: Test\n        run: pytest -q",
        "      - name: Test\n        continue-on-error: true\n        run: pytest -q"),
}


@pytest.mark.parametrize("label", sorted(MUTATIONS))
def test_workflow_spec_catches_dangerous_release_mutations(label: str, tmp_path: Path) -> None:
    assert _suite_catches(tmp_path, MUTATIONS[label]), f"mutação não detectada: {label}"
