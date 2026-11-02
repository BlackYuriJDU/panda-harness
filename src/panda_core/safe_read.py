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
        fd = os.open(target, os.O_RDONLY | _NO_FOLLOW)
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
