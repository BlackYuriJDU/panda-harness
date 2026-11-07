"""Read files that the workspace names, without following symlinks.

A corpus received from a third party can ship symlinks (git versions them).
Every file the workspace chooses -- config, policy, instructions, validator --
therefore has to be a plain file reached through plain directories, otherwise a
link such as `.panda/instructions.md -> ~/.ssh/id_ed25519` puts a local secret
into the model prompt (TRUST-005).

Two layers, because a link can be swapped after the first check:

1. `ensure_plain_path` walks every component below the workspace root with
   `lstat` and refuses a symlink anywhere in the chain.
2. `read_workspace_file` opens with `O_NOFOLLOW` where the platform has it and
   checks that the opened descriptor is a regular file.

Windows has no `O_NOFOLLOW`; junction handling there is not verified (WIN-001).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from panda_core.errors import ConfigError
from panda_core.redact import redact_text

DEFAULT_MAX_BYTES = 2_000_000

_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _refuse(path: Path, reason: str) -> ConfigError:
    return ConfigError(
        f"refusing workspace file {path}: {reason}",
        hint="replace the link with a regular file inside the workspace",
    )


def ensure_plain_path(root: Path, path: Path) -> Path:
    """Return `path` if no component below `root` is a symlink; else raise."""

    root = Path(os.path.abspath(root))
    target = Path(os.path.abspath(path))
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise _refuse(target, "it is outside the workspace") from None
    current = root
    for part in parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return target
        except OSError as exc:
            raise _refuse(target, str(exc)) from exc
        if stat.S_ISLNK(mode):
            raise _refuse(target, f"{current.name} is a symbolic link")
    return target


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NO_FOLLOW
# Opening relative to a directory descriptor is what closes the window between the
# lstat walk and the open (R2-209); Windows has no dir_fd, so the walk is all it gets.
_HAS_DIR_FD = os.open in os.supports_dir_fd and bool(_NO_FOLLOW)


def open_no_follow(root: Path, target: Path, flags: int = os.O_RDONLY) -> int:
    """Open `target` under `root` one component at a time, never following a link.

    Each directory is opened relative to the previous one with O_NOFOLLOW, so a
    parent swapped for a symlink after `ensure_plain_path` checked it is refused
    (ELOOP/ENOTDIR) instead of followed. Returns a file descriptor.
    """

    root = Path(os.path.abspath(root))
    target = Path(os.path.abspath(target))
    if not _HAS_DIR_FD:
        return os.open(target, flags | _NO_FOLLOW | getattr(os, "O_BINARY", 0))
    parts = target.relative_to(root).parts
    dir_fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        return os.open(parts[-1], flags | _NO_FOLLOW, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def read_workspace_file(
    root: Path,
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    redact: bool = False,
) -> str:
    """Read a workspace-named text file; at most `max_bytes` bytes are returned."""

    target = ensure_plain_path(root, path)
    try:
        fd = open_no_follow(root, target)
    except OSError as exc:
        if target.is_symlink():
            raise _refuse(target, "it is a symbolic link") from exc
        raise
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _refuse(target, "it is not a regular file")
        data = handle.read(max_bytes)
    text = data.decode("utf-8", errors="replace")
    return redact_text(text) if redact else text
