"""Wave 11: workspace discovery, init and user state do not follow the corpus."""

from __future__ import annotations

import json
import os
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


# --------------------------------------------------------------------------- TOCTOU (RC-10)
@POSIX
def test_parent_directory_swapped_after_lstat_walk_is_not_followed(tmp_path: Path) -> None:
    from panda_core.safe_read import ensure_plain_path, open_no_follow

    root = tmp_path / "w"
    (root / ".panda").mkdir(parents=True)
    target = root / ".panda" / "instructions.md"
    target.write_text("plain", encoding="utf-8")
    ensure_plain_path(root, target)  # the walk passes
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "instructions.md").write_text("SYNTH_SECRET_SWAP", encoding="utf-8")
    (root / ".panda").rename(root / ".panda-real")
    (root / ".panda").symlink_to(outside, target_is_directory=True)  # swapped after the walk
    with pytest.raises(OSError):
        open_no_follow(root, target)


@POSIX
def test_grep_worker_does_not_follow_a_file_swapped_to_a_secret(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "w"
    root.mkdir()
    note = root / "note.md"
    note.write_text("hello\n", encoding="utf-8")
    info = os.lstat(note)
    secret = tmp_path / "secret.env"
    secret.write_text("hello SYNTH_SECRET_GREP_SWAP\n", encoding="utf-8")
    note.unlink()
    note.symlink_to(secret)  # swapped after the policy check
    worker = Path(__file__).parents[2] / "src" / "panda_core" / "grep_worker.py"
    request = {"pattern": "hello", "flags": 0, "fixed": True, "limit": 10,
               "max_bytes": 100_000, "files": [["note.md", info.st_dev, info.st_ino]]}
    out = subprocess.run([sys.executable, "-I", str(worker)], cwd=root, input=json.dumps(request),
                         capture_output=True, text=True, check=True).stdout
    assert "SYNTH_SECRET_GREP_SWAP" not in out
    assert json.loads(out)["scanned"] == 0


@POSIX
def test_file_lock_does_not_follow_symlink(tmp_path: Path) -> None:
    from panda_core.atomic import file_lock

    target = tmp_path / "victim.txt"
    target.write_bytes(b"")
    lock = tmp_path / "x.lock"
    lock.symlink_to(target)
    with pytest.raises(OSError), file_lock(lock):
        pass
    assert target.read_bytes() == b""


@POSIX
def test_cockpit_crash_log_does_not_follow_a_corpus_symlink(tmp_path: Path) -> None:
    from panda_cli.cockpit import _open_private_log

    target = tmp_path / "victim.txt"
    target.write_text("keep", encoding="utf-8")
    log = tmp_path / "cockpit.log"
    log.symlink_to(target)
    with pytest.raises(OSError):
        _open_private_log(log)
    assert target.read_text(encoding="utf-8") == "keep"


@POSIX
def test_study_list_does_not_follow_a_symlinked_index(workspace, tmp_path: Path) -> None:
    import asyncio

    from panda_cli.runtime import ChatRuntime

    index = workspace.root / "corpus" / "studies" / "INDEX.md"
    outside = tmp_path / "outside.md"
    outside.write_text("| SYNTH_SECRET_INDEX | x |\n", encoding="utf-8")
    index.unlink()
    index.symlink_to(outside)
    runtime = ChatRuntime(workspace, label="w11")
    try:
        result = asyncio.run(runtime.tools.call("study_list", {}))
    finally:
        runtime.close()
    assert "SYNTH_SECRET_INDEX" not in result.content and not result.ok


def test_study_list_obeys_a_read_deny_on_the_registry(workspace) -> None:
    import asyncio

    from panda_cli.runtime import ChatRuntime

    rules = json.loads(workspace.policy_path.read_text(encoding="utf-8"))
    rules["rules"].append({"id": "hide-index", "actions": ["read"],
                           "resources": ["corpus/studies/*"], "effect": "deny", "risk": "high"})
    workspace.policy_path.write_text(json.dumps(rules), encoding="utf-8")
    runtime = ChatRuntime(workspace, label="w11")
    try:
        result = asyncio.run(runtime.tools.call("study_list", {}))
    finally:
        runtime.close()
    assert not result.ok and "gse160208" not in result.content


# --------------------------------------------------------------------------- user state
@POSIX
def test_credential_store_temp_file_is_never_world_readable(tmp_path: Path, monkeypatch) -> None:
    import stat as stat_module

    from panda_core import atomic

    seen: list[int] = []
    real_fdopen = os.fdopen

    def spy(fd, *args, **kwargs):
        seen.append(stat_module.S_IMODE(os.fstat(fd).st_mode))
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(atomic.os, "fdopen", spy)
    monkeypatch.setattr(os, "umask", os.umask)  # keep the process umask
    old = os.umask(0o022)
    try:
        atomic.atomic_write_json(tmp_path / "credentials.json", {"keys": {}})
    finally:
        os.umask(old)
    assert seen and all(mode & 0o077 == 0 for mode in seen)


@POSIX
def test_a_world_readable_credential_store_is_refused(isolated_credentials: Path) -> None:
    from panda_core.credentials import get_api_key
    from panda_core.errors import ConfigError

    isolated_credentials.write_text(json.dumps({"version": 1, "keys": {"x": "y" * 20}}),
                                    encoding="utf-8")
    isolated_credentials.chmod(0o644)
    with pytest.raises(ConfigError):
        get_api_key("opencode-go")


def test_backup_does_not_overwrite_same_second(tmp_path: Path) -> None:
    from panda_core.opencode import _backup

    source = tmp_path / "opencode.json"
    backups = tmp_path / "backups"
    source.write_text("first", encoding="utf-8")
    one = _backup(source, backups)
    source.write_text("second", encoding="utf-8")
    two = _backup(source, backups)
    assert one != two
    assert one.read_text(encoding="utf-8") == "first"


# --------------------------------------------------------------------------- config boundary
def _with_provider(workspace, **fields):
    from panda_core.workspace import Workspace

    path = workspace.root / ".panda" / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.setdefault("provider", {}).update(fields)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return lambda: Workspace.from_root(workspace.root)


@pytest.mark.parametrize(
    "fields",
    [{"name": "other-vault"}, {"context_window": 10_000_000}, {"max_output_tokens": 1_000_000},
     {"max_retries": 5}, {"timeout_seconds": 3600}],
)
def test_workspace_config_cannot_raise_budgets_or_pick_the_vault_key(workspace, fields) -> None:
    from panda_core.errors import ConfigError

    load = _with_provider(workspace, **fields)
    with pytest.raises(ConfigError):
        load()


def test_neg_workspace_config_may_lower_budgets(workspace) -> None:
    load = _with_provider(workspace, context_window=32_768, max_retries=1)
    assert load().config.provider.context_window == 32_768


def test_evidence_readers_are_bounded(workspace, monkeypatch) -> None:
    from panda_core import tools
    from panda_core.tools import ToolInputError, _read_jsonl

    big = workspace.root / "corpus" / "big.jsonl"
    big.write_text('{"id": "e1"}\n' * 100, encoding="utf-8")
    monkeypatch.setattr(tools, "MAX_DATA_BYTES", 64)
    with pytest.raises(ToolInputError):
        _read_jsonl(big)
