"""Subprocess helpers with consistent encoding, bounded timeouts, and shims."""

from __future__ import annotations

import contextlib
import locale
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    output_truncated: bool = False
    cancelled: bool = False

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


_SECRET_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GEMINI_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
    }
)
_SECRET_ENV_PREFIXES = ("PANDA_", "OPENCODE_", "OPENROUTER_")
_SECRET_ENV_SUFFIXES = (
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
    "_CREDENTIALS",
)


def scrubbed_env(*, keep_prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """The environment a child process may inherit, without credentials.

    Names that look like credentials are always removed. `keep_prefixes` only lifts
    the blanket removal of an entire prefix (such as `PANDA_`, which holds settings,
    not secrets); it never keeps a name that ends like a credential.
    """

    env: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in _SECRET_ENV_NAMES or upper.endswith(_SECRET_ENV_SUFFIXES):
            continue
        if upper.startswith(_SECRET_ENV_PREFIXES) and not upper.startswith(keep_prefixes):
            continue
        env[name] = value
    return env


DEFAULT_MAX_OUTPUT_BYTES = 4_000_000
_POLL_SECONDS = 0.05


def _pump(stream: Any, sink: bytearray, limit: int, over: threading.Event) -> None:
    """Copy a pipe into `sink`, keeping at most `limit` bytes (SEC-008)."""

    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            room = limit - len(sink)
            if room > 0:
                sink += chunk[:room]
            if len(chunk) > room:
                over.set()
    except (OSError, ValueError):
        return


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the command and everything it started.

    POSIX: the command leads its own session, so the whole group is signalled.
    Windows: `taskkill /T` walks the tree; Job Objects are not used and this path
    is not verified on a Windows host (WIN-001).
    """

    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],  # noqa: S607
                capture_output=True,
                timeout=5,
                check=False,
            )
        with contextlib.suppress(OSError):
            process.kill()
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def decode_output(data: bytes, *, fallback: str | None = None) -> str:
    """Decode a child's output: UTF-8 if it is UTF-8, else the system code page.

    On Windows a child writing to a pipe uses the ANSI code page (cp1252), so
    `olá` arrived as `ol\ufffd` (first run of the Windows CI on this branch).
    A UTF-8 sequence cut by the output cap only loses its tail.
    """

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start >= len(data) - 3 and exc.reason == "unexpected end of data":
            return data[: exc.start].decode("utf-8") + "\ufffd"
        encoding = fallback or (locale.getpreferredencoding(False) if os.name == "nt" else None)
        if encoding and encoding.lower().replace("-", "") not in {"utf8"}:
            return data.decode(encoding, errors="replace")
        return data.decode("utf-8", errors="replace")


def run_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    cancel: threading.Event | None = None,
    input_text: str | None = None,
) -> CommandResult:
    """Run a command with a hard timeout, an output cap and cancellation.

    The command runs in its own process group. A timeout, a set `cancel` event, or
    output beyond `max_output_bytes` per stream stops the whole group, so a
    grandchild cannot outlive the call. Children left behind by a command that
    exited normally are stopped too.
    """

    started = time.monotonic()
    group: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined,unused-ignore]
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    try:
        process = subprocess.Popen(  # noqa: S603
            list(command),
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **group,
        )
    except OSError as exc:
        return CommandResult(
            command=list(command),
            returncode=127,
            stdout="",
            stderr=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    over = threading.Event()
    out, err = bytearray(), bytearray()
    threads = [
        threading.Thread(target=_pump, args=(process.stdout, out, max_output_bytes, over)),
        threading.Thread(target=_pump, args=(process.stderr, err, max_output_bytes, over)),
    ]
    if input_text is not None:
        threads.append(threading.Thread(target=_feed, args=(process, input_text)))
    for thread in threads:
        thread.daemon = True
        thread.start()

    deadline = started + timeout if timeout else None
    timed_out = cancelled = False
    while True:
        try:
            process.wait(timeout=_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        if over.is_set():
            break
    _kill_tree(process)  # also reaps children left behind by a command that exited
    process.wait()
    for thread in threads:
        thread.join(timeout=2)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()

    def text(buffer: bytearray) -> str:
        return decode_output(bytes(buffer)).replace("\r\n", "\n")

    returncode = 124 if timed_out else 130 if cancelled else process.returncode
    return CommandResult(
        command=list(command),
        returncode=returncode,
        stdout=text(out),
        stderr=text(err),
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=timed_out,
        output_truncated=over.is_set(),
        cancelled=cancelled,
    )


def _feed(process: subprocess.Popen[bytes], text: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        if process.stdin is not None:
            process.stdin.write(text.encode("utf-8"))
            process.stdin.close()


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
