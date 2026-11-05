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
import shlex
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
    # What the command does, decided from its argv here and nowhere else (R2-500):
    # read, exec_local, publish, network, destructive or exec_unknown. `--auto`
    # reads this, never a rule id.
    effect_class: str = ""

    def __post_init__(self) -> None:
        if not self.effect_class:
            derived = _CLASS_BY_RULE.get(self.rule_id, "exec_unknown")
            object.__setattr__(self, "effect_class", derived)


_CLASS_BY_RULE = {
    "shell-git-readonly": "read",
    "shell-git-branch-list": "read",
    "shell-git-push": "publish",
    "shell-publish": "publish",
    "shell-network": "network",
    "shell-local": "exec_local",
    "shell-git-push-force": "destructive",
    "shell-git-reset-hard": "destructive",
    "shell-git-clean": "destructive",
    "shell-git-branch-delete": "destructive",
    "shell-rm-recursive": "destructive",
    "shell-find-delete": "destructive",
}
EFFECT_CLASSES = frozenset({*_CLASS_BY_RULE.values(), "exec_unknown"})

# Programs that publish or send something outside the machine. Never `--auto` (R2-204).
_PUBLISH_SUBCOMMANDS = {
    "twine": {"upload", "register"},
    "npm": {"publish", "unpublish", "deprecate", "dist-tag", "owner", "login", "adduser"},
    "pnpm": {"publish"},
    "yarn": {"publish", "npm"},
    "cargo": {"publish", "yank", "owner", "login"},
    "uv": {"publish"},
    "poetry": {"publish"},
    "flit": {"publish"},
    "hatch": {"publish"},
    "gem": {"push", "yank", "owner"},
    "docker": {"push", "login"},
    "podman": {"push", "login"},
}
_GH_READ_ONLY = {"view", "list", "status", "diff", "checks"}
_NETWORK = frozenset({"curl", "wget", "scp", "sftp", "ssh", "rsync", "nc", "ncat", "netcat",
                      "ftp", "telnet", "socat"})
# Local checks whose only effect is to run the workspace's own code and report.
_LOCAL_TOOLS = frozenset({"pytest", "py.test", "ruff", "mypy"})
_LOCAL_MODULES = frozenset({"pytest", "ruff", "mypy", "compileall", "unittest"})


# Wrappers that run the rest of argv as a command, with the options that take a
# value (so the value is not mistaken for the program). R2-103: `sudo -u root git
# push --force` and `timeout 60 git push --force` used to fall back to `ask`.
_WRAPPER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-C", "--close-from", "-h", "--host",
                       "-p", "--prompt", "-r", "--role", "-t", "--type", "-U", "--other-user",
                       "-D", "--chdir", "-R", "--chroot", "-T", "--command-timeout"}),
    "doas": frozenset({"-u", "-C"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "exec": frozenset({"-a"}),
    "command": frozenset(),
    "nohup": frozenset(),
    "setsid": frozenset(),
    "chrt": frozenset({"-T", "--sched-runtime", "-P", "--sched-period", "-D", "--sched-deadline"}),
    "taskset": frozenset({"--cpu-list"}),
    "xargs": frozenset({"-I", "-i", "-n", "--max-args", "-P", "--max-procs", "-d", "--delimiter",
                        "-E", "-e", "-L", "-l", "-s", "--max-chars", "-a", "--arg-file"}),
    "flock": frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}),
    "busybox": frozenset(),
}
# Wrappers with one positional before the command (duration, priority, CPU mask, lock file).
_WRAPPER_POSITIONAL = {"timeout": 1, "chrt": 1, "taskset": 1, "flock": 1}
# Options whose value is itself a command line.
_COMMAND_STRING_OPTIONS = {"env": {"-S", "--split-string"}, "flock": {"-c", "--command"},
                           "script": {"-c", "--command"}}
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "fish"})
_WRAPPERS = frozenset(_WRAPPER_VALUE_OPTIONS)
_SUFFIX_PROGRAMS = frozenset({"git", "rm", "find"})
_GIT_VALUE_GLOBALS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
)
_GIT_FLAG_GLOBALS = frozenset(
    {"--no-pager", "-P", "-p", "--paginate", "--bare", "--no-optional-locks", "--no-advice",
     "--glob-pathspecs", "--noglob-pathspecs", "--literal-pathspecs", "--icase-pathspecs",
     "--no-replace-objects", "--no-lazy-fetch"}
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


_SHORT_NAME = re.compile(r"([a-z0-9_]{1,6})~[0-9]+")


def _program(token: str) -> str:
    """The program a token names, across Windows spellings (R2-317).

    `git.exe.`, `git.exe `, `git.exe::$DATA`, `git.com` and the 8.3 name `GIT~1.EXE`
    all start git.
    """

    name = os.path.basename(token.replace("\\", "/")).lower()
    if "::" in name:
        name = name.split("::", 1)[0]
    name = name.rstrip(". ")
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].rstrip(". ")
            break
    short = _SHORT_NAME.fullmatch(name)
    return short.group(1) if short else name


def _long(arg: str, full: str, minimum: int) -> bool:
    """True when `arg` is `full` or an unambiguous git-style abbreviation of it."""

    name = arg.split("=", 1)[0]
    return name.startswith("--") and len(name) >= minimum and full.startswith(name)


def _short(args: Sequence[str], letter: str) -> bool:
    return any(a.startswith("-") and not a.startswith("--") and letter in a[1:] for a in args)


def _force(args: Sequence[str]) -> bool:
    return _short(args, "f") or any(
        a.split("=", 1)[0].startswith("--force") or _long(a, "--force", 3) for a in args
    )


def _unsafe_path(token: str) -> bool:
    text = token.replace("\\", "/")
    parts = text.split("/")
    return text.startswith("/") or (len(text) > 1 and text[1] == ":") or ".." in parts


def _git(args: Sequence[str]) -> ShellVerdict | None:
    index = 0
    config = False
    unknown_option = False
    settings: list[str] = []
    while index < len(args) and args[index].startswith("-"):
        token = args[index]
        name = token.split("=", 1)[0]
        if name in _GIT_VALUE_GLOBALS:
            if name in {"-c", "--config-env"}:
                config = True
                if "=" in token and token.startswith("--"):
                    settings.append(token.split("=", 1)[1].lower())
                elif index + 1 < len(args):
                    settings.append(args[index + 1].lower())
            index += 1 if ("=" in token and token.startswith("--")) else 2
        elif token in _GIT_FLAG_GLOBALS:
            index += 1
        else:
            # Keep reading: an unknown global must not hide `clean -fdx` (R2-202).
            unknown_option = True
            index += 1
    if index >= len(args):
        return None
    verdict = _git_subcommand(args[index], list(args[index + 1 :]), config, settings)
    if unknown_option and (verdict is None or SEVERITY[verdict.effect] < SEVERITY["ask"]):
        return ShellVerdict("ask", "medium", "unrecognised git option", "shell-git-option")
    return verdict


def _git_subcommand(
    sub: str, rest: Sequence[str], config: bool, settings: Sequence[str]
) -> ShellVerdict:
    positional = [a for a in rest if not a.startswith("-")]

    if sub == "push":
        if (
            _force(rest)
            or _long_any(rest, "--mirror", 3)
            or _long_any(rest, "--delete", 3)
            or _long_any(rest, "--prune", 3)
            or _short(rest, "d")
            or any(p[:1] in {"+", ":"} for p in positional)
            or any(item.startswith("remote.") and ".mirror" in item for item in settings)
        ):
            return ShellVerdict(
                "hard_deny",
                "critical",
                "history rewrite or remote deletion",
                "shell-git-push-force",
            )
        return ShellVerdict("ask", "high", "publishes commits", "shell-git-push")
    if sub == "reset" and _long_any(rest, "--hard", 3):
        return ShellVerdict("hard_deny", "critical", "discards local work", "shell-git-reset-hard")
    if sub == "clean" and _force(rest):
        return ShellVerdict("hard_deny", "critical", "deletes untracked files", "shell-git-clean")
    if sub == "send-email" or (sub == "remote" and "set-url" in rest and "--push" in rest):
        return ShellVerdict("ask", "high", "sends commits or redirects pushes", "shell-git-push")
    if sub == "branch":
        return _branch(rest, positional)
    if sub in _READ_ONLY:
        return _read_only(rest, config)
    return ShellVerdict("ask", "medium", "git command that can change the repository", "shell-git")


def _long_any(args: Sequence[str], full: str, minimum: int) -> bool:
    return any(_long(a, full, minimum) for a in args)


def _branch(rest: Sequence[str], positional: Sequence[str]) -> ShellVerdict:
    if _short(rest, "D") or _short(rest, "d") or _force(rest) or _long_any(rest, "--delete", 3):
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


def _split_command_string(text: str) -> list[list[str]]:
    """Commands in a shell string, split at `;`, `&&`, `||`, `|` and `&`."""

    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = text.split()
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= set(";&|()"):
            commands.append([])
        else:
            commands[-1].append(token)
    return [command for command in commands if command]


def _worst(verdicts: Sequence[ShellVerdict | None]) -> ShellVerdict | None:
    found = [v for v in verdicts if v is not None]
    if not found:
        return None
    return max(found, key=lambda item: SEVERITY[item.effect])


def _unwrap(program: str, args: list[str], depth: int) -> ShellVerdict | None:
    """Classify the command a wrapper runs, consuming its options and positionals."""

    value_options = _WRAPPER_VALUE_OPTIONS.get(program, frozenset())
    string_options = _COMMAND_STRING_OPTIONS.get(program, set())
    nested: list[ShellVerdict | None] = []
    index = 0
    while index < len(args):
        arg = args[index]
        name = arg.split("=", 1)[0]
        if arg == "--":
            index += 1
            break
        if program == "env" and "=" in arg and not arg.startswith("-"):
            index += 1  # NAME=VALUE
            continue
        if not arg.startswith("-") or arg == "-":
            break
        value = None
        if name in value_options or name in string_options:
            if "=" in arg:
                value = arg.split("=", 1)[1]
                index += 1
            else:
                value = args[index + 1] if index + 1 < len(args) else ""
                index += 2
        elif len(name) > 2 and not name.startswith("--") and name[:2] in value_options:
            value = name[2:]  # attached value: `-oL`, `-uroot`
            index += 1
        else:
            index += 1
        if value is not None and (name in string_options or name[:2] in string_options):
            nested += [classify_shell(cmd, depth + 1) for cmd in _split_command_string(value)]
    for _ in range(_WRAPPER_POSITIONAL.get(program, 0)):
        if index < len(args) and not (program == "flock" and args[index].startswith("-")):
            index += 1
    if program == "flock" and index < len(args) and args[index] in {"-c", "--command"}:
        if index + 1 < len(args):
            commands = _split_command_string(args[index + 1])
            nested += [classify_shell(cmd, depth + 1) for cmd in commands]
        return _worst(nested)
    nested.append(classify_shell(args[index:], depth + 1))
    return _worst(nested)


def _shell_string(args: list[str], depth: int) -> ShellVerdict | None:
    """`sh -c "git push --force"`: classify each command of the string."""

    for index, arg in enumerate(args):
        if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg):
            if index + 1 < len(args):
                commands = _split_command_string(args[index + 1])
                return _worst([classify_shell(cmd, depth + 1) for cmd in commands])
            return None
        if not arg.startswith("-"):
            return None
    return None


def classify_shell(argv: Sequence[str], _depth: int = 0) -> ShellVerdict | None:
    """Return a verdict for commands this module understands, else None."""

    if not argv or _depth > 4:
        return None
    program, args = _program(argv[0]), list(argv[1:])
    if program in _WRAPPERS:
        return _unwrap(program, args, _depth)
    if program in _SHELLS:
        return _shell_string(args, _depth)
    if program == "git":
        return _git(args)
    head = _classify_program(program, args)
    if head is not None:
        return head
    # An unknown program followed by a known dangerous one (`mywrap git push --force`)
    # may be a wrapper Panda does not know: never let it look harmless.
    for offset in range(1, len(argv)):
        if _program(argv[offset]) in _SUFFIX_PROGRAMS:
            suffix = classify_shell(argv[offset:], _depth + 1)
            if suffix is not None and SEVERITY[suffix.effect] >= SEVERITY["ask"]:
                return ShellVerdict(
                    "ask",
                    "high",
                    f"an unrecognised program runs `{_program(argv[offset])}`: {suffix.reason}",
                    "shell-wrapped-command",
                )
    return None


def _classify_program(program: str, args: list[str]) -> ShellVerdict | None:
    first = next((a for a in args if not a.startswith("-")), "")
    if program in _PUBLISH_SUBCOMMANDS and first in _PUBLISH_SUBCOMMANDS[program]:
        return ShellVerdict("ask", "high", f"{program} {first} publishes", "shell-publish")
    if program == "gh":
        words = [a for a in args if not a.startswith("-")]
        if not (len(words) >= 2 and words[1] in _GH_READ_ONLY):
            return ShellVerdict("ask", "high", "gh changes GitHub state", "shell-publish")
    if program in _NETWORK:
        return ShellVerdict("ask", "high", f"{program} talks to another machine", "shell-network")
    if program in _LOCAL_TOOLS or (
        program.startswith("python") and len(args) >= 2 and args[0] == "-m"
        and args[1] in _LOCAL_MODULES
    ):
        # Not an objection: the rules decide (the catch-all asks). The class lets
        # --auto approve it, because it only runs the workspace's own checks.
        return ShellVerdict("allow", "medium", "local check of the workspace", "shell-local")
    recursive = _short(args, "r") or _short(args, "R") or _long_any(args, "--recursive", 3)
    if program == "rm" and recursive:
        return ShellVerdict("hard_deny", "critical", "recursive deletion", "shell-rm-recursive")
    if program == "find":
        if "-delete" in args:
            return ShellVerdict("hard_deny", "critical", "find -delete", "shell-find-delete")
        if any(a in _FIND_ACTIONS for a in args):
            return ShellVerdict("ask", "high", "find runs or writes", "shell-find-exec")
    return None
