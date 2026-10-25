"""Subprocess helpers with consistent encoding, bounded timeouts, and shims."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SHIM_SUFFIXES = {".cmd", ".bat", ".ps1"}


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def split_command(value: str | Sequence[str]) -> list[str]:
    if isinstance(value, str):
        # POSIX parsing would eat backslashes in Windows paths.
        if os.name != "nt":
            return shlex.split(value, posix=True)
        parts = shlex.split(value, posix=False)
        # shlex keeps the surrounding quotes in non-POSIX mode, which breaks
        # executable resolution for paths such as `C:\Program Files\...`.
        return [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
            else part
            for part in parts
        ]
    return [str(part) for part in value]


def _expand_windows_shim(path: Path) -> list[str] | None:
    """Resolve an npm-style `.cmd` shim to the native executable it wraps.

    CreateProcess cannot execute a `.cmd` directly, and routing user-provided
    task text through `cmd.exe` would reintroduce shell parsing. The npm shims
    we care about delegate to a real `.exe`; resolve that instead. A shim we
    cannot resolve is reported as unavailable rather than executed unsafely.
    """

    if os.name != "nt" or path.suffix.lower() not in _SHIM_SUFFIXES:
        return [str(path)]
    if path.suffix.lower() == ".ps1":
        # PowerShell scripts cannot be executed without a shell either.
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    base = str(path.parent)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(("rem ", "@echo", "goto", "set ", "call ")):
            continue
        match = re.search(r'"([^"]+\.exe)"', stripped, re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1)
        candidate = candidate.replace("%dp0%", base).replace("%~dp0%", base)
        resolved = Path(candidate)
        if resolved.is_file():
            return [str(resolved)]
    return None


def resolve_command_prefix(value: str | Sequence[str]) -> list[str] | None:
    """Resolve a command to an executable prefix, expanding Windows shims."""

    command = split_command(value)
    if not command:
        return None
    candidate = command[0]
    resolved = shutil.which(candidate)
    if resolved:
        expanded = _expand_windows_shim(Path(resolved))
        if expanded is not None:
            return [*expanded, *command[1:]]
        return None
    path = Path(candidate)
    if path.is_file():
        expanded = _expand_windows_shim(path)
        if expanded is not None:
            return [*expanded, *command[1:]]
    return None


def resolve_executable(value: str | Sequence[str]) -> str | None:
    prefix = resolve_command_prefix(value)
    return prefix[0] if prefix else None


def run_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    import time

    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        return CommandResult(
            command=list(command),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=elapsed,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        stdout = (
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return CommandResult(
            command=list(command),
            returncode=124,
            stdout=stdout,
            stderr=stderr,
            duration_ms=elapsed,
            timed_out=True,
        )
    except OSError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return CommandResult(
            command=list(command),
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_ms=elapsed,
        )


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def command_version(value: str | Sequence[str], *, timeout: float = 5.0) -> str | None:
    prefix = resolve_command_prefix(value)
    if not prefix:
        return None
    result = run_command([*prefix, "--version"], timeout=timeout)
    if not result.ok:
        return None
    parsed = parse_version(result.stdout or result.stderr)
    if parsed is None:
        return None
    return ".".join(str(part) for part in parsed)
