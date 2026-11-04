"""Trilha B — session.py (append estrito, recover, cauda, is_closed) e treehash.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from panda_core.errors import SessionIntegrityError
from panda_core.session import SessionLog, event_hash
from panda_core.treehash import tree_hash


def _forge_last(path: Path, new_data: dict) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    record["data"] = new_data
    record.pop("hash")
    record["hash"] = event_hash(record)
    lines[-1] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_append_refuses_tail_with_same_seq_but_different_hash(tmp_path: Path) -> None:
    """Mata o mutante M12: o anchor nomeia seq N/hash H; a cauda é seq N/hash H'."""

    log = SessionLog.create(tmp_path, label="t")
    log.append("note", {"message": "aaaa"})
    _forge_last(log.path, {"message": "bbbbbbbbbbbbbbbb"})  # tamanho diferente
    fresh = SessionLog.open(log.path)
    with pytest.raises(SessionIntegrityError):
        fresh.append("note", {"message": "next"})


def test_long_lived_writer_refuses_same_size_forgery_of_the_tail(tmp_path: Path) -> None:
    """C-3: append estrito (anchor == cauda). O cache de cauda só é relido se o tamanho mudar."""

    log = SessionLog.create(tmp_path, label="t")
    log.append("note", {"message": "aaaa"})
    _forge_last(log.path, {"message": "bbbb"})  # mesmo tamanho em bytes
    with pytest.raises(SessionIntegrityError):
        log.append("note", {"message": "next"})


def test_service_does_not_append_after_session_end(workspace) -> None:  # type: ignore[no-untyped-def]
    from panda_core.service import PandaService

    log = SessionLog.create(workspace.sessions_dir, label="t")
    log.close("completed")
    service = PandaService(workspace, session=SessionLog.open(log.path))
    try:
        service.note("depois do fim")
    except Exception:  # noqa: BLE001 - recusar também é seguro
        pass
    assert SessionLog.open(log.path).verify().ok, SessionLog.open(log.path).verify().to_dict()


def test_tree_hash_sees_content_change_with_restored_mtime(tmp_path: Path) -> None:
    target = tmp_path / "corpus" / "claims.md"
    target.parent.mkdir(parents=True)
    target.write_text("value: 10\n", encoding="utf-8")
    cache: dict = {}
    before = tree_hash(tmp_path, cache).digest
    stat = target.stat()
    target.write_text("value: 99\n", encoding="utf-8")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert tree_hash(tmp_path, cache).digest != before


def test_tree_hash_covers_agent_writable_node_modules_under_corpus(tmp_path: Path) -> None:
    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "a.md").write_text("x\n", encoding="utf-8")
    before = tree_hash(tmp_path).digest
    module = tmp_path / "corpus" / "node_modules" / "corpus-helpers" / "index.js"
    module.parent.mkdir(parents=True)
    module.write_text("evil()\n", encoding="utf-8")
    assert tree_hash(tmp_path).digest != before
