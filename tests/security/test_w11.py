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


# --------------------------------------------------------------------------- ledger (W11)
def _log(tmp_path: Path):
    from panda_core.session import SessionLog

    return SessionLog.create(tmp_path / "sessions", label="w11")


def _rewrite_last_line(path: Path, mutate) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    mutate(record)
    lines[-1] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_long_lived_writer_refuses_same_size_forgery_of_the_tail(tmp_path: Path) -> None:
    from panda_core.errors import SessionIntegrityError

    log = _log(tmp_path)
    log.append("note", {"text": "AAAA"})
    size = log.path.stat().st_size
    _rewrite_last_line(log.path, lambda r: r["data"].update(text="BBBB"))
    assert log.path.stat().st_size == size  # same size: the old cache missed it
    with pytest.raises(SessionIntegrityError):
        log.append("note", {"text": "next"})


def test_append_refuses_tail_with_same_seq_but_different_hash(tmp_path: Path) -> None:
    from panda_core.errors import SessionIntegrityError

    log = _log(tmp_path)
    log.append("note", {"text": "a"})
    _rewrite_last_line(log.path, lambda r: r.update(hash="0" * 64))
    with pytest.raises(SessionIntegrityError):
        log.append("note", {"text": "b"})


def test_no_append_after_session_end(tmp_path: Path) -> None:
    from panda_core.errors import SessionIntegrityError

    log = _log(tmp_path)
    log.close("completed")
    with pytest.raises(SessionIntegrityError):
        log.append("note", {"text": "late"})
    assert log.verify().ok


def _unanchored(tmp_path: Path, event_type: str, ts: str | None = None):
    """A log whose last event was appended without updating the anchor."""

    log = _log(tmp_path)
    log.append("note", {"text": "anchored"})
    anchor = log.anchor_path.read_text(encoding="utf-8")
    log.append(event_type, {"x": 1})
    log.anchor_path.write_text(anchor, encoding="utf-8")
    if ts is not None:
        from panda_core.session import event_hash

        def backdate(record):
            record["ts"] = ts
            body = {k: v for k, v in record.items() if k != "hash"}
            record["hash"] = event_hash(body)

        _rewrite_last_line(log.path, backdate)
    from panda_core.session import SessionLog

    return SessionLog.open(log.path) if hasattr(SessionLog, "open") else log


def test_recover_adopts_a_plausible_crash_event(tmp_path: Path) -> None:
    log = _unanchored(tmp_path, "tool.call")
    assert log.recover() is True


@pytest.mark.parametrize(
    ("event_type", "ts"),
    [("approval.decision", None), ("note", "2020-01-01T00:00:00Z"),
     ("note", "2999-01-01T00:00:00Z")],
)
def test_recover_refuses_backdated_or_approval_events(
    tmp_path: Path, event_type: str, ts: str | None
) -> None:
    from panda_core.errors import SessionIntegrityError

    log = _unanchored(tmp_path, event_type, ts)
    with pytest.raises(SessionIntegrityError):
        log.recover()


def test_a_rehashed_forged_tail_is_caught_by_the_anchor(tmp_path: Path) -> None:
    """Kills mutant M12: only the anchor hash comparison catches a valid re-hash."""

    from panda_core.errors import SessionIntegrityError
    from panda_core.session import event_hash

    log = _log(tmp_path)
    log.append("note", {"text": "real"})

    def forge(record):
        record["data"] = {"text": "forged"}
        body = {k: v for k, v in record.items() if k != "hash"}
        record["hash"] = event_hash(body)

    _rewrite_last_line(log.path, forge)
    with pytest.raises(SessionIntegrityError, match="anchor"):
        log.append("note", {"text": "next"})
