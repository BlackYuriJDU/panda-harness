"""Workspace discovery and path normalization."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from panda_core.errors import ConfigError
from panda_core.workspace import Workspace, find_workspace_root, is_writable_dir


def test_find_root_from_nested_directory(workspace: Workspace) -> None:
    nested = workspace.root / "corpus" / "studies"
    assert find_workspace_root(nested) == workspace.root
    assert Workspace.load(nested).root == workspace.root


def test_find_root_by_validator(fake_corpus: Path) -> None:
    assert find_workspace_root(fake_corpus) == fake_corpus
    loaded = Workspace.load(fake_corpus)
    assert loaded.initialized is False
    assert loaded.config.model


def test_load_without_workspace(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Workspace.load(tmp_path)
    with pytest.raises(ConfigError):
        Workspace.load(tmp_path, require_init=True)


def test_from_root_require_init(fake_corpus: Path) -> None:
    with pytest.raises(ConfigError):
        Workspace.from_root(fake_corpus, require_init=True)
    workspace = Workspace.from_root(fake_corpus)
    assert workspace.initialized is False


def test_relative_resource_normalizes_traversal(workspace: Workspace) -> None:
    assert workspace.relative_resource("corpus/studies/x.md") == "corpus/studies/x.md"
    assert workspace.relative_resource("corpus/../README.md") == "README.md"
    assert workspace.relative_resource("./corpus/./x.md") == "corpus/x.md"


def test_relative_resource_keeps_outside_absolute(workspace: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    normalized = workspace.relative_resource(outside)
    assert normalized == outside.resolve().as_posix()
    assert not normalized.startswith("corpus/")


def test_ensure_runtime_dirs(workspace: Workspace) -> None:
    assert workspace.sessions_dir.is_dir()
    workspace.ensure_runtime_dirs()
    assert workspace.sessions_dir.is_dir()


def test_describe(workspace: Workspace) -> None:
    info = workspace.describe()
    assert info["root"] == str(workspace.root)
    assert info["initialized"] is True
    assert info["validator_exists"] is True
    assert "python" in info["tools"]
    assert "git" in info


def test_git_state_without_repo(fake_corpus: Path) -> None:
    workspace = Workspace.from_root(fake_corpus)
    assert workspace.git_state()["repo"] is False


def test_git_state_uses_one_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    workspace = Workspace.from_root(root)
    calls: list[list[str]] = []

    class Result:
        stdout = "# branch.oid abc123\n# branch.head main\n1 .M N... 100644 0 0 x\n"

    def fake_run(args: list[str], **kwargs: object) -> Result:
        calls.append(args)
        return Result()

    monkeypatch.setattr("panda_core.workspace.subprocess.run", fake_run)

    state = workspace.git_state()

    assert state == {"repo": True, "commit": "abc123", "branch": "main", "dirty": True}
    assert calls == [["git", "status", "--porcelain=v2", "--branch"]]


def test_node_version_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANDA_HOME", str(tmp_path / "home"))
    workspace = Workspace.from_root(tmp_path)
    calls: list[tuple[object, ...]] = []

    def fake_version(*args: object, **kwargs: object) -> str:
        calls.append(args)
        return "24.0.0"

    monkeypatch.setattr("panda_core.workspace.command_version", fake_version)

    assert workspace.tool_versions()["node"] == "24.0.0"
    assert workspace.tool_versions()["node"] == "24.0.0"
    assert len(calls) == 1

def test_is_writable_dir_skips_protected_windows_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific protected roots")
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setattr(
        "panda_core.workspace.tempfile.mkstemp",
        lambda **kwargs: pytest.fail("write probe attempted in a protected root"),
    )

    assert is_writable_dir(system32) is False
