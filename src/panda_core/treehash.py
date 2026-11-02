"""A content hash of the corpus, for the ledger (LED-001).

The ledger proves the sequence of events; this adds the state they left behind. The
hash covers every regular file under `corpus/`, as (relative path, SHA-256 of the
bytes), in sorted order, so two copies of the same corpus hash alike wherever they
live. The ledger, `.panda/` and anything outside `corpus/` are not part of it.

`git write-tree` is not used: it hashes the index, which misses unstaged work, and
it writes objects into `.git`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

SCOPE = "corpus"
MAX_FILES = 20_000
MAX_FILE_BYTES = 64 * 1024 * 1024
_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv"})


@dataclass(frozen=True)
class TreeHash:
    digest: str
    files: int
    truncated: bool


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def tree_hash(root: Path, cache: dict[str, tuple[int, int, str]] | None = None) -> TreeHash:
    """Hash `root/corpus`. `cache` maps path to (mtime_ns, size, sha) to skip rehashing."""

    base = root / SCOPE
    entries: list[tuple[str, str]] = []
    truncated = False
    if base.is_dir():
        for current, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            for name in sorted(names):
                path = Path(current) / name
                if path.is_symlink() or not path.is_file():
                    continue
                if len(entries) >= MAX_FILES:
                    truncated = True
                    break
                stat = path.stat()
                if stat.st_size > MAX_FILE_BYTES:
                    truncated = True
                    continue
                key = str(path)
                hit = cache.get(key) if cache is not None else None
                if hit and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
                    digest = hit[2]
                else:
                    digest = file_sha256(path)
                    if cache is not None:
                        cache[key] = (stat.st_mtime_ns, stat.st_size, digest)
                entries.append((path.relative_to(root).as_posix(), digest))
            if truncated and len(entries) >= MAX_FILES:
                break
    outer = hashlib.sha256()
    for relative, digest in sorted(entries):
        outer.update(f"{relative}\0{digest}\n".encode())
    return TreeHash(outer.hexdigest(), len(entries), truncated)
