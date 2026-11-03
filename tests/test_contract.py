"""docs/CONTRACT.md must say what the code does (DOC-001).

A frozen contract that disagrees with the code is a debt of trust. These tests read the
contract and compare it with the commands the CLI registers, the ledger events the code
can write, the hard denies the default policy applies, and the version the docs name.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import typer.main

from panda_cli.main import app
from panda_core.policy import PolicyEngine, default_policy
from panda_core.version import __version__

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = (ROOT / "docs" / "CONTRACT.md").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- commands
def code_commands() -> set[str]:
    def walk(command, prefix: list[str]) -> set[str]:
        subcommands = getattr(command, "commands", None)
        if not subcommands:
            return {" ".join(prefix)}
        found: set[str] = set()
        for name, sub in subcommands.items():
            if not getattr(sub, "hidden", False):
                found |= walk(sub, [*prefix, name])
        return found

    return walk(typer.main.get_command(app), ["panda"])


def documented_commands() -> set[str]:
    section = CONTRACT.split("## 4. Commands", 1)[1].split("### 4.1", 1)[0]
    names: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("|---") or "Command" in line.split("|")[1]:
            continue
        for span in re.findall(r"`([^`]+)`", line.split("|")[1]):
            tokens = []
            for token in span.split():
                if token.startswith(("<", '"', "[", "-")):
                    break
                tokens.append(token)
            if tokens and tokens[0] == "panda":
                names.add(" ".join(tokens))
    return names


def test_every_registered_command_is_in_the_contract_and_the_reverse() -> None:
    # The row `panda` / `panda chat` documents one command: bare `panda` opens the chat.
    documented = (documented_commands() - {"panda"}) | {"panda chat"}
    code = code_commands()
    assert code - documented == set(), f"in the code but not in CONTRACT §4: {code - documented}"
    assert documented - code == set(), f"in CONTRACT §4 but not in the code: {documented - code}"


# --------------------------------------------------------------------------- events
EVENT_CALLS = {"_log", "append", "_record", "_append_locked"}


def code_events() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in EVENT_CALLS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                value = node.args[0].value
                if re.fullmatch(r"[a-z]+(\.[a-z_]+)+", value) or value in {"error", "note"}:
                    found.add(value)
    return found


def test_every_event_the_code_can_write_is_documented() -> None:
    missing = sorted(name for name in code_events() if f"`{name}`" not in CONTRACT)
    assert not missing, f"events written by the code but missing from CONTRACT §9: {missing}"


def test_the_documented_event_names_are_the_ones_the_code_uses() -> None:
    for wrong in ("user.message", "assistant.message"):
        assert wrong not in CONTRACT and wrong not in ARCHITECTURE, wrong
    assert "`message.user`" in CONTRACT and "`message.assistant`" in CONTRACT


# --------------------------------------------------------------------------- hard denies
HARD_DENIED = [
    ("read", ".env"),
    ("read", "keys/server.pem"),
    ("read", ".ssh/id_ed25519"),
    ("shell", "git push --force origin main"),
    ("shell", "git reset --hard HEAD"),
    ("shell", "rm -rf corpus"),
    ("release", "publish v1"),
]


@pytest.mark.parametrize(("action", "resource"), HARD_DENIED)
def test_what_the_contract_calls_a_hard_deny_is_one(action: str, resource: str) -> None:
    assert PolicyEngine(default_policy()).decide(action, resource).effect == "hard_deny"


def test_the_contract_names_the_categories_it_hard_denies() -> None:
    section = CONTRACT.split("## 8. Policy and approvals", 1)[1].split("## 9.", 1)[0]
    for phrase in ("force-push", "history rewrites", "recursive deletion", "release publishing"):
        assert phrase in section, phrase


# --------------------------------------------------------------------------- versions
def test_the_readme_names_the_current_version() -> None:
    status = "\n".join((ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:14])
    assert __version__ in status, "README status line does not name the current version"


def test_the_security_policy_supports_the_current_line() -> None:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    line = ".".join(__version__.split(".")[:2])
    assert f"`{line}.x`" in text


def test_the_contract_header_names_the_current_version() -> None:
    assert __version__ in CONTRACT.split("## 1.", 1)[0]


def test_pyproject_and_version_module_agree() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject


# --------------------------------------------------------------------------- privacy claim
def test_the_private_flag_is_documented_as_what_it_really_covers() -> None:
    lines = CONTRACT.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("- `--private`"))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(("- ", "#"))), len(lines)
    )
    bullet = " ".join(" ".join(lines[start:end]).split()).lower()  # one line, lower case
    assert "assistant" in bullet and "tool arguments" in bullet and "tool.call" in bullet
