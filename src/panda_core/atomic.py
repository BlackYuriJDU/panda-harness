"""Small filesystem primitives used by the session ledger and config writers."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive advisory lock, cross-platform.

    The lock file is separate from the data file so readers never need to take
    a lock. It is intentionally tiny; the kernel releases it if the process dies.
    """

    ensure_dir(path.parent)
    handle = open(path, "a+b")  # noqa: SIM115 - explicit lifecycle below
    try:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            deadline = time.monotonic() + timeout
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"could not acquire lock: {path}") from exc
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                with contextlib.suppress(OSError):
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
    finally:
        handle.close()


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write text atomically, never leaving a partially written destination."""

    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2, mode: int = 0o600) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=False) + "\n",
        mode=mode,
    )


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def canonical_json(value: Any) -> str:
    """Stable JSON for hashing: sorted keys, no insignificant whitespace."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
