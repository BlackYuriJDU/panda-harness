"""Subprocess command parsing and Windows shim behavior."""

from __future__ import annotations

import os

import pytest

from panda_core.process import split_command


def test_split_command_accepts_a_list() -> None:
    assert split_command(["panda", "mcp", "serve"]) == ["panda", "mcp", "serve"]


def test_split_command_accepts_a_simple_string() -> None:
    assert split_command("opencode run --standalone") == [
        "opencode",
        "run",
        "--standalone",
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows shlex mode")
def test_split_command_strips_quotes_around_a_windows_path() -> None:
    command = split_command('"C:\\Program Files\\Panda\\panda.exe" mcp serve')
    assert command == [r"C:\Program Files\Panda\panda.exe", "mcp", "serve"]
