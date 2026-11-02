"""Classify a shell command by its effect, from argv tokens (SEC-001, SEC-007).

Matching glob patterns against `" ".join(argv)` cannot model options: reordering
`git -C . reset --hard`, or `git diff --output=README.md`, slipped past it, while
`git push origin fix-foo` was refused for containing "-f". This module reads the
tokens instead. It can only *escalate* what the policy rules decided
(allow < ask < deny < hard_deny); it never lowers an effect, so a workspace
policy can only get stricter.

Tokens that make a command read-only are a closed set: anything this module does
not recognise on a `git` command needs an approval.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

SEVERITY = {"allow": 0, "ask": 1, "deny": 2, "hard_deny": 3}


@dataclass(frozen=True)
class ShellVerdict:
    effect: str
    risk: str
    reason: str
    rule_id: str
    # Positional tokens of a read-only git command; the policy engine checks each
    # one against the `read` rules (so `git diff -- .env` is refused).
    read_paths: tuple[str, ...] = ()


_WRAPPERS = frozenset({"env", "command", "nohup", "nice", "time", "sudo", "doas", "exec"})
_GIT_VALUE_GLOBALS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)
_GIT_FLAG_GLOBALS = frozenset(
    {"--no-pager", "-P", "-p", "--paginate", "--bare", "--no-optional-locks", "--no-advice"}
)
_READ_ONLY = frozenset({"status", "diff", "log", "show"})
_FORBIDDEN_READ_FLAGS = (("--output", 5), ("--no-index", 7), ("--ext-diff", 5))
_FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fls"})
_SAFE_FLAG = re.compile(
    r"^(?:-[psvbwzq]|-[0-9]+|-U[0-9]+|-n[0-9]*|-u[a-z]*|-[SG]\S+"
    r"|--(?:stat|shortstat|numstat|name-only|name-status|oneline|graph|decorate|no-decorate"
    r"|abbrev-commit|no-abbrev-commit|no-color|color|cached|staged|short|branch|porcelain"
    r"|patch|no-patch|summary|all|merges|no-merges|first-parent|reverse|word-diff|check"
    r"|raw|relative|full-index|no-renames|find-renames|minimal|patience|histogram|abbrev"
    r"|date|max-count|since|until|author|grep|diff-filter|unified|format|pretty|skip"
    r"|untracked-files|ignored)(?:=\S*)?)$"
)
_SAFE_BRANCH = re.compile(
    r"^(?:-[arvli]|-vv|--(?:list|all|remotes|show-current|no-color|color|verbose|ignore-case"
    r"|contains|merged|no-merged|sort|format|points-at|abbrev)(?:=\S*)?)$"
)


def _program(token: str) -> str:
    name = os.path.basename(token.replace("\\", "/")).lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _long(arg: str, full: str, minimum: int) -> bool:
    """True when `arg` is `full` or an unambiguous git-style abbreviation of it."""

    name = arg.split("=", 1)[0]
    return name.startswith("--") and len(name) >= minimum and full.startswith(name)


def _short(args: Sequence[str], letter: str) -> bool:
    return any(a.startswith("-") and not a.startswith("--") and letter in a[1:] for a in args)


def _force(args: Sequence[str]) -> bool:
    return _short(args, "f") or any(
        a.split("=", 1)[0].startswith("--force") or _long(a, "--force", 5) for a in args
    )


def _unsafe_path(token: str) -> bool:
    text = token.replace("\\", "/")
    parts = text.split("/")
    return text.startswith("/") or (len(text) > 1 and text[1] == ":") or ".." in parts


def _git(args: Sequence[str]) -> ShellVerdict | None:
    index = 0
    config = False
    while index < len(args) and args[index].startswith("-"):
        token = args[index]
        name = token.split("=", 1)[0]
        if name in _GIT_VALUE_GLOBALS:
            config = config or name in {"-c", "--config-env"}
            index += 1 if "=" in token else 2
        elif token in _GIT_FLAG_GLOBALS:
            index += 1
        else:
            return ShellVerdict("ask", "medium", "unrecognised git option", "shell-git-option")
    if index >= len(args):
        return None
    sub, rest = args[index], list(args[index + 1 :])
    positional = [a for a in rest if not a.startswith("-")]

    if sub == "push":
        if (
            _force(rest)
            or _long_any(rest, "--mirror", 5)
            or _long_any(rest, "--delete", 5)
            or _short(rest, "d")
            or any(p[:1] in {"+", ":"} for p in positional)
        ):
            return ShellVerdict(
                "hard_deny",
                "critical",
                "history rewrite or remote deletion",
                "shell-git-push-force",
            )
        return ShellVerdict("ask", "high", "publishes commits", "shell-git-push")
    if sub == "reset" and _long_any(rest, "--hard", 4):
        return ShellVerdict("hard_deny", "critical", "discards local work", "shell-git-reset-hard")
    if sub == "clean" and _force(rest):
        return ShellVerdict("hard_deny", "critical", "deletes untracked files", "shell-git-clean")
    if sub == "branch":
        return _branch(rest, positional)
    if sub in _READ_ONLY:
        return _read_only(rest, config)
    return ShellVerdict("ask", "medium", "git command that can change the repository", "shell-git")


def _long_any(args: Sequence[str], full: str, minimum: int) -> bool:
    return any(_long(a, full, minimum) for a in args)


def _branch(rest: Sequence[str], positional: Sequence[str]) -> ShellVerdict:
    if _short(rest, "D") or _short(rest, "d") or _force(rest) or _long_any(rest, "--delete", 5):
        return ShellVerdict("hard_deny", "critical", "deletes a branch", "shell-git-branch-delete")
    flags = [a for a in rest if a.startswith("-")]
    listing = any(a in {"-l", "--list"} or a.startswith("--list") for a in flags)
    if all(_SAFE_BRANCH.match(a) for a in flags) and (listing or not positional):
        return ShellVerdict("allow", "low", "lists branches", "shell-git-branch-list")
    return ShellVerdict("ask", "medium", "creates or edits a branch", "shell-git-branch")


def _read_only(rest: Sequence[str], config: bool) -> ShellVerdict:
    effect, reason, rule = "allow", "read-only git command", "shell-git-readonly"
    paths: list[str] = []
    after_separator = False
    for arg in rest:
        if arg == "--" and not after_separator:
            after_separator = True
            continue
        if after_separator or not arg.startswith("-"):
            if ":" in arg or _unsafe_path(arg):
                effect, reason, rule = "ask", "path outside the workspace", "shell-git-path"
            else:
                paths.append(arg)
        elif any(_long(arg, full, size) for full, size in _FORBIDDEN_READ_FLAGS):
            return ShellVerdict(
                "deny", "high", "writes or reads outside the workspace", "shell-git-output"
            )
        elif not _SAFE_FLAG.match(arg) and effect == "allow":
            effect, reason, rule = "ask", "unrecognised option", "shell-git-flag"
    if config and effect == "allow":
        effect, reason, rule = "ask", "git configuration override", "shell-git-config"
    risk = "low" if effect == "allow" else "medium"
    return ShellVerdict(effect, risk, reason, rule, tuple(paths))


def classify_shell(argv: Sequence[str], _depth: int = 0) -> ShellVerdict | None:
    """Return a verdict for commands this module understands, else None."""

    if not argv:
        return None
    program, args = _program(argv[0]), list(argv[1:])
    if program in _WRAPPERS and _depth < 3:
        index = 0
        while index < len(args) and (
            args[index].startswith("-") or "=" in args[index] or args[index].isdigit()
        ):
            index += 1
        return classify_shell(args[index:], _depth + 1)
    if program == "git":
        return _git(args)
    recursive = _short(args, "r") or _short(args, "R") or _long_any(args, "--recursive", 5)
    if program == "rm" and recursive:
        return ShellVerdict("hard_deny", "critical", "recursive deletion", "shell-rm-recursive")
    if program == "find":
        if "-delete" in args:
            return ShellVerdict("hard_deny", "critical", "find -delete", "shell-find-delete")
        if any(a in _FIND_ACTIONS for a in args):
            return ShellVerdict("ask", "high", "find runs or writes", "shell-find-exec")
    return None
