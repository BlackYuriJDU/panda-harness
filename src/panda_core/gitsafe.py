"""Run read-only git without letting the repository choose a program (RC-05, RC-06).

A corpus can arrive with its own `.git`, and an agent can write
`corpus/sub/.git/config` with write_file. Repository config and attributes name
programs that read-only commands run: `core.fsmonitor` (status), `diff.external`
and textconv drivers (diff, show, log -p), clean filters (any command that looks
at the worktree), hooks such as `post-index-change` (any command that refreshes
the index), pagers and gpg. Overriding a few keys is not enough: filter names are
free-form, so they are neutralised by name after reading `.git/info/attributes`,
the one attribute source that `--attr-source` does not replace (verified on git
2.43).

`harden()` rebuilds the command from scratch: explicit `--git-dir`/`--work-tree`,
system and global config off, every inherited `GIT_*` variable dropped, and
pathspec excludes so that content commands never print files the read policy
denies (R2-102). Anything it cannot reason about (wrappers, `-C`, a `.git` file,
an old git) is returned as a hazard, which the caller turns into an approval.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from panda_core.safe_read import read_workspace_file

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_ATTR_SOURCE_MIN = (2, 42)
_ATTRIBUTES_MAX = 256_000
_FILTER_NAME = re.compile(r"(?<![!\-\w])filter=([^\s]+)")
_KEEP_GLOBALS = frozenset({"--no-pager", "-P", "--no-optional-locks", "--no-advice"})
_CONTENT = frozenset({"diff", "show"})
_PATCH_FLAGS = frozenset({"-p", "-u", "--patch", "--patch-with-stat", "--patch-with-raw", "-U"})


@dataclass(frozen=True)
class HardenedGit:
    """The command Panda really runs, or why it will not run it unattended."""

    argv: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    hazard: str | None = None


def _program(token: str) -> str:
    name = os.path.basename(token.replace("\\", "/")).lower()
    return name[:-4] if name.endswith(".exe") else name


@lru_cache(maxsize=4)
def _git_version(executable: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no repository involved
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)", result.stdout)
    return (int(match.group(1)), int(match.group(2))) if match else None


def find_repository(root: Path, cwd: Path) -> tuple[Path | None, str | None]:
    """The repository that contains `cwd`, searched only inside the workspace.

    Returns (worktree, None), or (None, hazard). (None, None) means no repository:
    git then must not climb above the workspace to find one.
    """

    root = Path(os.path.abspath(root))
    current = Path(os.path.abspath(cwd))
    while True:
        candidate = current / ".git"
        try:
            is_link = candidate.is_symlink()
            exists = candidate.exists() or is_link
        except OSError:
            return None, "cannot inspect .git"
        if exists:
            if is_link or not candidate.is_dir():
                return None, f"{_relative(root, candidate)} is a link or a .git file"
            return current, None
        if current == root or root not in current.parents:
            return None, None
        current = current.parent


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def filter_names(root: Path, git_dir: Path) -> tuple[set[str], str | None]:
    """Filter drivers named by `.git/info/attributes` (macros included)."""

    path = git_dir / "info" / "attributes"
    try:
        if not path.exists() and not path.is_symlink():
            return set(), None
        text = read_workspace_file(root, path, max_bytes=_ATTRIBUTES_MAX + 1)
    except Exception:  # noqa: BLE001 - unreadable attributes are a hazard, never ignored
        return set(), "cannot read .git/info/attributes"
    if len(text) > _ATTRIBUTES_MAX:
        return set(), ".git/info/attributes is too large to inspect"
    return set(_FILTER_NAME.findall(text)), None


def git_env(base: Mapping[str, str], *, ceiling: Path | None = None) -> dict[str, str]:
    env = {key: value for key, value in base.items() if not key.upper().startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    if ceiling is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(ceiling)
    return env


def _overrides(filters: Iterable[str]) -> list[str]:
    pairs = [
        "core.fsmonitor=false",
        f"core.hooksPath={os.devnull}",
        "diff.external=",
        "core.pager=cat",
        f"core.attributesFile={os.devnull}",
        "protocol.allow=never",
        "log.showSignature=false",
        f"gpg.program={os.devnull}",
        f"gpg.ssh.program={os.devnull}",
        f"gpg.x509.program={os.devnull}",
        "submodule.recurse=false",
        "diff.ignoreSubmodules=all",
        "status.submoduleSummary=false",
    ]
    for name in sorted(filters):
        pairs += [
            f"filter.{name}.clean=",
            f"filter.{name}.smudge=",
            f"filter.{name}.process=",
            f"filter.{name}.required=false",
        ]
    argv: list[str] = []
    for pair in pairs:
        argv += ["-c", pair]
    return argv


def exclude_pathspecs(patterns: Iterable[str]) -> list[str]:
    """Git pathspecs that drop every path matching a read-denied policy glob.

    Default git pathspecs use wildmatch without FNM_PATHNAME, so `*` crosses `/`
    like the policy's fnmatch; `icase` matches the policy's case folding.
    """

    specs = []
    for pattern in dict.fromkeys(patterns):
        if pattern and pattern != "*":
            specs.append(f":(top,exclude,icase){pattern}")
    return specs


def harden(
    argv: Sequence[str],
    *,
    root: Path,
    cwd: Path,
    base_env: Mapping[str, str],
    denied_reads: Iterable[str] = (),
) -> HardenedGit:
    """Rebuild an allowed read-only git command so the repository cannot run code."""

    if not argv or _program(argv[0]) != "git" or argv[0] not in {"git", "git.exe"}:
        return HardenedGit(hazard="only a plain `git` command runs unattended")
    executable = shutil.which("git")
    if executable is None:
        return HardenedGit(hazard="git is not installed")
    version = _git_version(executable)
    if version is None or version < _ATTR_SOURCE_MIN:
        return HardenedGit(hazard="git older than 2.42 cannot ignore corpus attributes")

    index = 1
    while index < len(argv) and argv[index].startswith("-"):
        if argv[index] not in _KEEP_GLOBALS:
            return HardenedGit(hazard=f"git option {argv[index]} changes where git looks")
        index += 1
    if index >= len(argv):
        return HardenedGit(hazard="no git subcommand")
    sub, rest = argv[index], list(argv[index + 1 :])

    worktree, hazard = find_repository(root, cwd)
    if hazard:
        return HardenedGit(hazard=hazard)
    env = git_env(base_env, ceiling=Path(os.path.abspath(root)).parent)
    command = ["git", "--no-pager", "--no-optional-locks"]
    if worktree is not None:
        git_dir = worktree / ".git"
        filters, hazard = filter_names(root, git_dir)
        if hazard:
            return HardenedGit(hazard=hazard)
        command += [f"--git-dir={git_dir}", f"--work-tree={worktree}"]
    else:
        filters = set()
    command += [f"--attr-source={EMPTY_TREE}", *_overrides(filters), sub]

    content = sub in _CONTENT or (sub == "log" and any(_is_patch(a) for a in rest))
    if sub in _CONTENT or sub == "log":
        command += ["--no-ext-diff", "--no-textconv"]
    if not content:
        return HardenedGit(argv=(*command, *rest), env=env)

    if "--" in rest:
        split = rest.index("--")
        options, paths = rest[:split], rest[split + 1 :]
    else:
        options, paths = [], []
        for token in rest:
            is_path = not token.startswith("-") and (cwd / token).exists()
            (paths if is_path else options).append(token)
    excludes = exclude_pathspecs(denied_reads)
    return HardenedGit(argv=(*command, *options, "--", *(paths or ["."]), *excludes), env=env)


def _is_patch(token: str) -> bool:
    return token in _PATCH_FLAGS or token.startswith("-U") or token.startswith("--unified")
