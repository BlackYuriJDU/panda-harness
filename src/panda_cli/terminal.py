"""Restore the terminal after the full-screen cockpit exits.

Textual restores the terminal on a clean shutdown, but a crash skips its
teardown: the console keeps the alternate screen active and mouse tracking
enabled, so the shell receives SGR mouse reports such as `[<35;69;23M` as
keystrokes. This module is the last-resort restore path. ANSI sequences do the
portable work; Win32 calls (through `ctypes` only) put the Windows console
input mode back, because PowerShell otherwise keeps `ENABLE_MOUSE_INPUT`.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
from typing import Any

ANSI_MOUSE_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"
ANSI_ALT_SCREEN_OFF = "\x1b[?1049l"
ANSI_CURSOR_ON = "\x1b[?25h"
TERMINAL_RESET = ANSI_MOUSE_OFF + ANSI_ALT_SCREEN_OFF + ANSI_CURSOR_ON

STD_INPUT_HANDLE = 0xFFFFFFF6  # (DWORD)-10, as the Win32 API expects it
ENABLE_MOUSE_INPUT = 0x0010


def _kernel32() -> Any:
    """Return kernel32 on Windows, else None. Import-safe everywhere."""
    if sys.platform != "win32":
        return None
    try:
        return ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return None


def _input_handle() -> int | None:
    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    with contextlib.suppress(Exception):
        kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        return int(handle) if handle else None
    return None


def snapshot_console_mode() -> int | None:
    """Original stdin console mode, or None when there is no console."""
    kernel32 = _kernel32()
    handle = _input_handle()
    if kernel32 is None or handle is None:
        return None
    mode = ctypes.c_uint32()
    with contextlib.suppress(Exception):
        kernel32.GetConsoleMode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.GetConsoleMode.restype = ctypes.c_int
        if kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)):
            return int(mode.value)
    return None


def restore_console_mode(mode: int | None) -> None:
    """Put the stdin console mode back.

    With no snapshot, clear `ENABLE_MOUSE_INPUT` in the current mode: that is
    the flag that turns mouse reports into input events after a crash.
    """
    kernel32 = _kernel32()
    handle = _input_handle()
    if kernel32 is None or handle is None:
        return
    target = mode
    if target is None:
        current = snapshot_console_mode()
        if current is None:
            return
        target = current & ~ENABLE_MOUSE_INPUT
    with contextlib.suppress(Exception):
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.SetConsoleMode.restype = ctypes.c_int
        kernel32.SetConsoleMode(ctypes.c_void_p(handle), ctypes.c_uint32(target))


def reset_terminal(mode: int | None = None) -> None:
    """Undo mouse tracking, the alternate screen, and a hidden cursor.

    Best effort by construction: this runs in a `finally` after a crash, so it
    must never raise. `mode` is the value from `snapshot_console_mode`.
    """
    stream = sys.stdout
    if stream is None:
        return
    with contextlib.suppress(AttributeError, OSError, ValueError):
        if stream.isatty():
            stream.write(TERMINAL_RESET)
            stream.flush()
    restore_console_mode(mode)


__all__ = [
    "ANSI_ALT_SCREEN_OFF",
    "ANSI_CURSOR_ON",
    "ANSI_MOUSE_OFF",
    "TERMINAL_RESET",
    "reset_terminal",
    "restore_console_mode",
    "snapshot_console_mode",
]
