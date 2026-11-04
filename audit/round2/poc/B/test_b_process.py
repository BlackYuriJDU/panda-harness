"""Trilha B — process.py (run_command/_kill_tree/scrubbed_env). Afirma o resultado SEGURO."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from panda_core.process import run_command, scrubbed_env

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # zumbi conta como morto
    with contextlib.suppress(OSError):
        status = Path(f"/proc/{pid}/stat").read_text().split()[2]
        if status == "Z":
            return False
    return True


def _spawn_escaper(tmp_path: Path) -> list[str]:
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen(['sleep', '30'], start_new_session=True,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(tmp_path / 'pid')!r}, 'w').write(str(p.pid))\n"
    )
    return [sys.executable, "-c", code]


@POSIX
def test_grandchild_in_new_session_does_not_outlive_normal_exit(tmp_path: Path) -> None:
    run_command(_spawn_escaper(tmp_path), timeout=10)
    pid = int((tmp_path / "pid").read_text())
    try:
        time.sleep(0.2)
        assert not _alive(pid), "neto em nova sessão sobreviveu ao run_command (C-8)"
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


@POSIX
def test_grandchild_in_new_session_does_not_outlive_timeout(tmp_path: Path) -> None:
    argv = _spawn_escaper(tmp_path)
    argv[-1] += "import time; time.sleep(30)\n"
    result = run_command(argv, timeout=1)
    assert result.timed_out
    pid = int((tmp_path / "pid").read_text())
    try:
        time.sleep(0.2)
        assert not _alive(pid), "neto em nova sessão sobreviveu ao timeout (C-8)"
    finally:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    "name",
    ["PGPASSWORD", "DATABASE_URL", "SENTRY_DSN", "OPENAI_APIKEY", "GITHUB_PAT", "MYSQL_PWD",
     "NPM_CONFIG__AUTH", "AZURE_STORAGE_CONNECTION_STRING", "SLACK_WEBHOOK_URL"],
)
def test_child_environment_drops_credential_like_variables(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, "SYNTH_SECRET_ENV_VALUE")
    assert name not in scrubbed_env(), name
