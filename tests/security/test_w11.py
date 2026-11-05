"""Wave 11: workspace discovery, init and user state do not follow the corpus."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and symlinks")


def _plant(directory: Path) -> None:
    (directory / ".panda").mkdir(parents=True, exist_ok=True)
    (directory / ".panda" / "config.json").write_text(json.dumps({}), encoding="utf-8")


# --------------------------------------------------------------------------- R2-300
def test_a_workspace_above_home_is_not_adopted(tmp_path: Path, monkeypatch) -> None:
    from panda_core.workspace import find_workspace_root

    shared = tmp_path / "shared"
    home = shared / "home" / "alice"
    project = home / "notes"
    project.mkdir(parents=True)
    _plant(shared)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert find_workspace_root(project) is None


def test_neg_a_workspace_inside_home_is_still_found(tmp_path: Path, monkeypatch) -> None:
    from panda_core.workspace import find_workspace_root

    home = tmp_path / "home"
    corpus = home / "corpus"
    deep = corpus / "corpus" / "claims"
    deep.mkdir(parents=True)
    _plant(corpus)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert find_workspace_root(deep) == corpus.resolve()


@POSIX
def test_ancestor_workspace_owned_by_other_user_is_not_adopted(tmp_path: Path, monkeypatch) -> None:
    from panda_core import workspace as module

    outer = tmp_path / "outer"
    inner = outer / "work" / "new"
    inner.mkdir(parents=True)
    _plant(outer)
    monkeypatch.setenv("HOME", "/nonexistent-home")
    real = module._owned_by_user
    monkeypatch.setattr(module, "_owned_by_user", lambda p: p != outer.resolve() and real(p))
    assert module.find_workspace_root(inner) is None


def test_the_filesystem_root_is_never_adopted(monkeypatch) -> None:
    from panda_core.workspace import _candidates

    start = Path.cwd()
    assert Path(start.anchor) not in _candidates(start)[1:]


# --------------------------------------------------------------------------- R2-305
@POSIX
@pytest.mark.parametrize("name", ["policy.json", "config.json"])
def test_init_does_not_create_files_through_a_dangling_symlink(
    tmp_path: Path, name: str
) -> None:
    from panda_core.errors import ConfigError
    from panda_core.init import initialize_workspace

    root = tmp_path / "corpus-root"
    (root / ".panda").mkdir(parents=True)
    outside = tmp_path / "outside" / "planted.json"
    outside.parent.mkdir()
    (root / ".panda" / name).symlink_to(outside)
    with pytest.raises(ConfigError):
        initialize_workspace(root)
    assert not outside.exists()


@POSIX
def test_init_force_does_not_overwrite_or_back_up_a_link_target(
    workspace, tmp_path: Path
) -> None:
    from panda_core.errors import ConfigError
    from panda_core.init import initialize_workspace

    root = workspace.root
    secret = tmp_path / "secret.txt"
    secret.write_text("SYNTH_SECRET_INIT_7", encoding="utf-8")
    instructions = root / ".panda" / "instructions.md"
    instructions.unlink()
    instructions.symlink_to(secret)
    with pytest.raises(ConfigError):
        initialize_workspace(root, force=True)
    assert secret.read_text(encoding="utf-8") == "SYNTH_SECRET_INIT_7"
    backups = root / ".panda" / "backups"
    assert not backups.exists() or "SYNTH_SECRET_INIT_7" not in "".join(
        p.read_text(encoding="utf-8", errors="replace") for p in backups.rglob("*") if p.is_file()
    )


@POSIX
def test_init_refuses_a_linked_panda_directory(tmp_path: Path) -> None:
    from panda_core.errors import ConfigError
    from panda_core.init import initialize_workspace

    root = tmp_path / "corpus-root"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ".panda").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ConfigError):
        initialize_workspace(root)
    assert list(elsewhere.iterdir()) == []
