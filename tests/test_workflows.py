"""The release pipeline keeps write access to the last step, after tests (SUP-001).

Read as text, by job block, so the check needs no YAML dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

GITHUB = Path(__file__).resolve().parents[1] / ".github"


def text(name: str) -> str:
    return (GITHUB / name).read_text(encoding="utf-8")


def jobs(name: str) -> dict[str, str]:
    """Job name -> its block of text (two-space indented keys under `jobs:`)."""

    body = text(f"workflows/{name}").split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):\n", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def head(name: str) -> str:
    return text(f"workflows/{name}").split("\njobs:\n", 1)[0]


def test_release_workflow_is_read_only_by_default() -> None:
    assert re.search(r"(?m)^permissions:\n  contents: read$", head("release.yml"))
    assert "contents: write" not in head("release.yml")


def test_only_the_publish_job_can_write() -> None:
    writers = [name for name, block in jobs("release.yml").items() if "contents: write" in block]
    assert writers == ["publish"]


def test_publish_runs_after_the_build_which_runs_after_the_tests() -> None:
    blocks = jobs("release.yml")
    assert re.search(r"(?m)^    needs: test$", blocks["build"])
    assert re.search(r"(?m)^    needs: build$", blocks["publish"])


def test_the_release_tests_run_the_same_checks_as_ci() -> None:
    block = jobs("release.yml")["test"]
    for command in ("ruff check", "mypy", "pytest"):
        assert command in block


def test_publishing_only_happens_for_version_tags_and_stays_a_draft() -> None:
    block = jobs("release.yml")["publish"]
    assert "if: startsWith(github.ref, 'refs/tags/v')" in block
    assert "draft: true" in block


def test_ci_audits_the_dependencies() -> None:
    assert "pip-audit" in jobs("ci.yml")["audit"]


def test_ci_keeps_read_only_permissions() -> None:
    assert re.search(r"(?m)^permissions:\n  contents: read$", head("ci.yml"))
    assert "contents: write" not in text("workflows/ci.yml")


def test_dependabot_watches_actions_and_python() -> None:
    config = text("dependabot.yml")
    assert 'package-ecosystem: "github-actions"' in config
    assert 'package-ecosystem: "pip"' in config
