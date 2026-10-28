"""Console entry point isolated from the process working directory.

Python puts the current directory on ``sys.path`` for ``python -m`` (and for
some launchers), so a stray ``typer.py`` or ``pydantic.py`` in the cwd can
shadow a dependency before the CLI is imported.  Drop only the cwd entry,
then import the real CLI.  ``--version`` is answered without importing Typer,
pydantic, or the rest of the heavy stack.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

_VERSION_FLAGS = frozenset({"--version", "-V"})


def _path_key(path: str) -> str:
    """Normalize a path for comparison without requiring it to exist."""

    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _sanitize_sys_path() -> list[str]:
    """Remove the cwd (and its ``""`` spelling) from ``sys.path``.

    Editable-install and site-packages entries are left untouched.  Returns
    the entries that were removed so callers can log or assert on them.
    """

    try:
        cwd = os.getcwd()
    except OSError:
        return []

    cwd_keys = {_path_key(cwd)}
    real_cwd = os.path.realpath(cwd)
    if real_cwd != cwd:
        cwd_keys.add(_path_key(real_cwd))

    removed: list[str] = []
    kept: list[str] = []
    for entry in sys.path:
        if entry == "":
            removed.append(entry)
            continue
        entry_keys = {_path_key(entry)}
        real_entry = os.path.realpath(entry)
        if real_entry != entry:
            entry_keys.add(_path_key(real_entry))
        if entry_keys & cwd_keys:
            removed.append(entry)
        else:
            kept.append(entry)

    sys.path[:] = kept
    return removed


def _wants_version(argv: Sequence[str]) -> bool:
    return any(argument in _VERSION_FLAGS for argument in argv)


def _announce_boot(argv: Sequence[str]) -> None:
    """Print one line before the heavy imports so the console is never blank.

    Only the bare `panda` invocation: subcommands may emit JSON or line output.
    stderr keeps stdout clean and is replaced when Textual takes the screen.
    """

    if argv:
        return
    try:
        if not sys.stderr.isatty():
            return
    except (AttributeError, ValueError):
        return
    from panda_core.version import __version__

    sys.stderr.write(f"panda {__version__} · loading cockpit…\n")
    sys.stderr.flush()


def main() -> None:
    _sanitize_sys_path()

    if _wants_version(sys.argv[1:]):
        from panda_core.version import __version__

        sys.stdout.write(f"panda {__version__}\n")
        raise SystemExit(0)

    _announce_boot(sys.argv[1:])

    from panda_cli.main import main as cli_main

    cli_main()
