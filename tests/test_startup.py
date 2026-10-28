"""Startup robustness: an unwritable cwd uses the Panda home workspace."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from panda_cli.main import _chat_workspace, app
from panda_core.errors import ConfigError

runner = CliRunner()


def test_startup_home_fallback_when_cwd_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "unwritable"
    cwd.mkdir()
    monkeypatch.setenv("PANDA_HOME", str(home))
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(
        "panda_core.workspace.is_writable_dir",
        lambda path: Path(path).resolve() != cwd.resolve(),
    )

    workspace, error = _chat_workspace(None)

    assert error is None
    assert workspace.root == home.resolve()
    assert workspace.initialized is True
    assert (home / ".panda" / "config.json").is_file()
    assert (home / ".panda" / "sessions").is_dir()
    assert not (cwd / ".panda").exists()


def test_home_fallback_unwritable_is_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "unwritable"
    cwd.mkdir()
    monkeypatch.setenv("PANDA_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(cwd)
    monkeypatch.setattr("panda_core.workspace.is_writable_dir", lambda path: False)

    with pytest.raises(ConfigError) as excinfo:
        _chat_workspace(None)

    assert "home workspace" in excinfo.value.message
    assert excinfo.value.hint
    assert not (cwd / ".panda").exists()


def test_main_callback_handles_panda_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**kwargs: object) -> None:
        raise ConfigError("workspace root is not writable", hint="set PANDA_HOME")

    monkeypatch.setattr("panda_cli.main._start_chat", fail)

    result = runner.invoke(app, [])

    assert result.exit_code == 2
    assert "workspace root is not writable" in result.output
    assert "Traceback" not in result.output


def test_chat_root_unwritable_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    monkeypatch.setattr("panda_core.workspace.is_writable_dir", lambda path: False)

    result = runner.invoke(app, ["chat", "--root", str(root), "--plain"])

    assert result.exit_code == 2
    assert "workspace root is not writable" in result.output
    assert "Traceback" not in result.output
    assert not (root / ".panda").exists()


def test_normal_writable_cwd_still_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PANDA_HOME", raising=False)
    cwd = tmp_path / "plain-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    workspace, error = _chat_workspace(None)

    assert workspace.root == cwd.resolve()
    assert workspace.initialized is False
    assert error is not None
    assert error.code == "config_error"

def test_chat_workspace_skips_protected_windows_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-specific protected roots")
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    home = tmp_path / "home"
    real_mkstemp = tempfile.mkstemp

    def guarded_mkstemp(*, prefix: str, dir: Path | str) -> tuple[int, str]:
        if Path(dir).resolve() == system32.resolve():
            raise AssertionError("write probe attempted in a protected root")
        return real_mkstemp(prefix=prefix, dir=dir)

    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setenv("PANDA_HOME", str(home))
    monkeypatch.chdir(system32)
    monkeypatch.setattr("panda_core.workspace.tempfile.mkstemp", guarded_mkstemp)

    workspace, error = _chat_workspace(None)

    assert error is None
    assert workspace.root == home.resolve()
    assert not (system32 / ".panda").exists()
