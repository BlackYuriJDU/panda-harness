"""Entry point hardening: cwd shadowing and fast version output."""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
from pathlib import Path

import pytest

from panda_cli import entry
from panda_core.version import __version__

HEAVY_MODULES = {"panda_cli", "pydantic", "rich", "textual", "typer"}
SCRIPT = Path(sys.executable).with_name("panda.exe" if os.name == "nt" else "panda")


def _shadow_dir(tmp_path: Path) -> Path:
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "typer.py").write_text(
        "raise RuntimeError('shadowed typer')\n", encoding="utf-8"
    )
    (shadow / "pydantic.py").write_text(
        "raise RuntimeError('shadowed pydantic')\n", encoding="utf-8"
    )
    return shadow


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONSAFEPATH", None)
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_sanitize_sys_path_drops_cwd_and_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    site = tmp_path / "site-packages"
    site.mkdir()
    cwd_variant = f"{cwd}{os.sep}"
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(sys, "path", ["", str(cwd), cwd_variant, str(site), str(tmp_path)])

    removed = entry._sanitize_sys_path()

    assert removed == ["", str(cwd), cwd_variant]
    assert sys.path == [str(site), str(tmp_path)]


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_shortcut_avoids_heavy_imports(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["panda", flag])
    monkeypatch.setattr(sys, "path", list(sys.path))
    real_import = builtins.__import__

    def guarded(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".")[0] in HEAVY_MODULES:
            raise AssertionError(f"heavy import attempted: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)

    with pytest.raises(SystemExit) as excinfo:
        entry.main()

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"panda {__version__}"


def test_module_entry_survives_shadowing_cwd(tmp_path: Path) -> None:
    shadow = _shadow_dir(tmp_path)

    result = _run([sys.executable, "-m", "panda_cli", "--version"], shadow)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"panda {__version__}"
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(not SCRIPT.exists(), reason="console script is not installed")
def test_console_script_survives_shadowing_cwd(tmp_path: Path) -> None:
    shadow = _shadow_dir(tmp_path)

    result = _run([str(SCRIPT), "--version"], shadow)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"panda {__version__}"
    assert "Traceback" not in result.stderr


class _FakeTty:
    def __init__(self, tty: bool = True) -> None:
        self.text = ""
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def write(self, value: str) -> None:
        self.text += value

    def flush(self) -> None:
        pass


def test_boot_banner_for_a_bare_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTty()
    monkeypatch.setattr(sys, "stderr", fake)

    entry._announce_boot([])

    assert fake.text == f"panda {__version__} · loading cockpit…\n"


def test_boot_banner_is_silent_for_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTty()
    monkeypatch.setattr(sys, "stderr", fake)

    entry._announce_boot(["models"])

    assert fake.text == ""


def test_boot_banner_is_silent_without_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTty(tty=False)
    monkeypatch.setattr(sys, "stderr", fake)

    entry._announce_boot([])

    assert fake.text == ""
