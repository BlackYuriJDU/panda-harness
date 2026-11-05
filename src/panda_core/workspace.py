"""Workspace discovery and accessors for `.panda/config.json`."""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from panda_core.atomic import atomic_write_json
from panda_core.config import (
    DEFAULT_POLICY,
    DEFAULT_SESSIONS,
    DEFAULT_VALIDATOR,
    PandaConfig,
    default_config,
    load_config,
)
from panda_core.errors import ConfigError
from panda_core.process import command_version, parse_version
from panda_core.safe_read import read_workspace_file

CONFIG_DIR = ".panda"
CONFIG_FILE = "config.json"
POLICY_FILE = "policy.json"
SESSIONS_DIR = "sessions"
BACKUPS_DIR = "backups"
INSTRUCTIONS_FILE = "instructions.md"

_GIT_FILE_MAX = 1_000_000
_GIT_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_GIT_BRANCH_REF = re.compile(r"refs/heads/[A-Za-z0-9._/+-]{1,200}")


def _candidates(start: Path) -> list[Path]:
    """Directories searched for a workspace: `start`, then ancestors Panda may adopt.

    R2-300 (git's CVE-2022-24765 class): climbing every ancestor adopted a `.panda/`
    planted in a shared directory (`/tmp`, `C:\\`). The search now stops at the
    user's home, never adopts a filesystem root above `start`, and on POSIX never
    climbs into a directory owned by another user.
    """

    current = start.resolve()
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    found = [current]
    if current == home:
        return found
    for parent in current.parents:
        if parent == Path(parent.anchor):
            break
        if not _owned_by_user(parent):
            break
        found.append(parent)
        if parent == home:
            break
    return found


def _owned_by_user(path: Path) -> bool:
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # Windows: ownership is not in st_uid; the root/home limits apply
        return True
    try:
        return path.stat().st_uid == getuid()
    except OSError:
        return False


def find_workspace_root(start: Path | None = None) -> Path | None:
    """Find the nearest ancestor with `.panda/config.json`, else a validator."""

    start = (start or Path.cwd()).resolve()
    if start.is_file():
        start = start.parent
    candidates = _candidates(start)
    for candidate in candidates:
        if (candidate / CONFIG_DIR / CONFIG_FILE).is_file():
            return candidate
    for candidate in candidates:
        if (candidate / DEFAULT_VALIDATOR).is_file():
            return candidate
    return None


PANDA_HOME_ENV = "PANDA_HOME"
HOME_DIR = "panda"


def home_workspace_path() -> Path:
    """Return the user-level workspace used when the cwd is not writable."""

    override = os.environ.get(PANDA_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if base:
            return Path(base) / HOME_DIR / "home"
        return Path.home() / CONFIG_DIR / "home"
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if data_home:
        return Path(data_home) / HOME_DIR / "home"
    return Path.home() / ".local" / "share" / HOME_DIR / "home"


def _is_protected_dir(path: Path) -> bool:
    """Return True for Windows roots where a write probe may block."""

    if os.name != "nt":
        return False
    try:
        target = path.resolve()
    except OSError:
        return True
    for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        try:
            root = Path(value).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            continue
        return True
    return False


def is_writable_dir(path: Path) -> bool:
    """Probe real writability by creating and removing a temp file.

    Protected Windows roots are reported unwritable without touching them:
    ``mkstemp`` inside ``System32`` can block for tens of seconds behind
    Windows security/AV instead of failing fast.
    """

    if _is_protected_dir(path):
        return False

    try:
        if not path.is_dir():
            return False
        descriptor, name = tempfile.mkstemp(prefix=".panda-write-probe-", dir=path)
    except OSError:
        return False
    with contextlib.suppress(OSError):
        os.close(descriptor)
    with contextlib.suppress(OSError):
        os.unlink(name)
    return True


@dataclass(frozen=True)
class Workspace:
    root: Path
    config: PandaConfig
    initialized: bool = False

    @classmethod
    def load(cls, start: Path | None = None, *, require_init: bool = False) -> Workspace:
        start = (start or Path.cwd()).resolve()
        if start.is_file():
            start = start.parent
        candidates = _candidates(start)

        for candidate in candidates:
            config_path = candidate / CONFIG_DIR / CONFIG_FILE
            if config_path.is_file():
                return cls(root=candidate, config=load_config(config_path), initialized=True)

        if require_init:
            raise ConfigError(
                "no Panda workspace found",
                hint="run `panda init` in the corpus root, or pass --root",
            )

        for candidate in candidates:
            if (candidate / DEFAULT_VALIDATOR).is_file():
                return cls(root=candidate, config=default_config(), initialized=False)

        raise ConfigError(
            "not a Panda workspace and no corpus validator was found",
            hint=(
                "run `panda init --root <corpus-root>`, or pass --root; "
                "a corpus root contains corpus/validation/validate.mjs"
            ),
        )

    @classmethod
    def from_root(cls, root: Path, *, require_init: bool = False) -> Workspace:
        resolved = root.resolve()
        config_path = resolved / CONFIG_DIR / CONFIG_FILE
        if config_path.is_file():
            return cls(root=resolved, config=load_config(config_path), initialized=True)
        if require_init:
            raise ConfigError(
                f"workspace is not initialized: {resolved}",
                hint="run `panda init --root <corpus-root>`",
            )
        return cls(root=resolved, config=default_config(), initialized=False)

    @property
    def config_dir(self) -> Path:
        return self.root / CONFIG_DIR

    @property
    def config_path(self) -> Path:
        return self.config_dir / CONFIG_FILE

    @property
    def policy_path(self) -> Path:
        configured = Path(self.config_dir / POLICY_FILE)
        if self.initialized:
            return configured
        return self.root / DEFAULT_POLICY

    @property
    def sessions_dir(self) -> Path:
        return self.root / DEFAULT_SESSIONS

    @property
    def backups_dir(self) -> Path:
        return self.root / CONFIG_DIR / BACKUPS_DIR

    @property
    def instructions_path(self) -> Path:
        return self.root / self.config.instructions

    @property
    def validator_path(self) -> Path:
        return self.root / self.config.validator

    @property
    def opencode_config_path(self) -> Path:
        return self.root / self.config.opencode.config_path

    def ensure_runtime_dirs(self) -> None:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"cannot create the runtime directories under {self.root}",
                hint=(
                    "use a writable workspace with --root, or set PANDA_HOME "
                    "to a writable directory"
                ),
            ) from exc

    def relative_resource(self, value: str | Path) -> str:
        """Normalize a path to the workspace-relative form used by policy rules.

        The path is resolved before matching so `corpus/../README.md` cannot
        masquerade as a corpus write. Paths that escape the workspace stay
        absolute, where the corpus allow rules cannot match them.
        """

        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        try:
            relative = path.resolve().relative_to(self.root.resolve())
        except (OSError, ValueError):
            return path.resolve().as_posix()
        return relative.as_posix()

    def git_state(self) -> dict:
        """Branch and commit of the workspace repository, read without running git.

        A corpus can arrive with its own `.git` (zip, copy). Its config names
        programs (clean/smudge filters, hooks, fsmonitor) that any `git status`
        would run, and the probe used to pass them the whole environment
        (R2-106). So nothing is executed here: HEAD and the ref are read as
        plain files, and `dirty` is `None` (unknown) because only git can tell.
        A `.git` file (worktree, submodule) points elsewhere and is not followed.
        """

        git_dir = self.root / ".git"
        unknown = {"repo": True, "commit": None, "branch": None, "dirty": None}
        try:
            mode = os.lstat(git_dir).st_mode
        except FileNotFoundError:
            return {"repo": False}
        except OSError:
            return unknown
        if not stat.S_ISDIR(mode):
            return unknown
        head = self._git_file("HEAD")
        if head is None:
            return unknown
        head = head.strip()
        if _GIT_OID.fullmatch(head):
            return {**unknown, "commit": head}
        if not head.startswith("ref: "):
            return unknown
        ref = head[len("ref: ") :].strip()
        if not _GIT_BRANCH_REF.fullmatch(ref) or ".." in ref or "//" in ref:
            return unknown
        branch = ref[len("refs/heads/") :]
        commit = self._resolve_ref(ref)
        return {**unknown, "branch": branch, "commit": commit}

    def _git_file(self, relative: str) -> str | None:
        try:
            return read_workspace_file(
                self.root, self.root / ".git" / relative, max_bytes=_GIT_FILE_MAX
            )
        except (ConfigError, OSError, ValueError):
            return None

    def _resolve_ref(self, ref: str) -> str | None:
        loose = self._git_file(ref)
        if loose is not None:
            value = loose.strip()
            return value if _GIT_OID.fullmatch(value) else None
        packed = self._git_file("packed-refs")
        if packed is None:
            return None
        for line in packed.splitlines():
            oid, _, name = line.partition(" ")
            if name.strip() == ref and _GIT_OID.fullmatch(oid):
                return oid
        return None

    def tool_versions(self, *, probe: bool = True) -> dict:
        """Tool versions for diagnostics.

        With `probe=False` nothing is executed: the node version comes from the
        user-scoped cache or is `None`. Read-only commands (`status`, session
        metadata) use this so that opening a corpus never runs a binary
        (TRUST-002); `doctor` and `validate` are where node runs.
        """

        return {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "node": self._cached_node_version(probe=probe),
        }

    def record_node_version(self, text: str) -> None:
        """Store a node version that `doctor` has just measured.

        Read-only commands report it without ever running the binary.
        """

        parsed = parse_version(text)
        if parsed is None:
            return
        cache_path = home_workspace_path().parent / "cache" / "tool-versions.json"
        entry = {
            "command": self.config.node,
            "version": ".".join(str(part) for part in parsed),
            "checked_at": int(time.time()),
        }
        with contextlib.suppress(OSError):
            atomic_write_json(cache_path, {"node": entry})

    def _cached_node_version(self, *, probe: bool = True) -> str | None:
        """Avoid spawning node on every boot; refresh at most once a week."""

        command = self.config.node
        cache_path = home_workspace_path().parent / "cache" / "tool-versions.json"
        now = int(time.time())
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        entry = raw.get("node") if isinstance(raw, dict) else None
        if (
            isinstance(entry, dict)
            and entry.get("command") == command
            and isinstance(entry.get("checked_at"), int)
            and 0 <= now - entry["checked_at"] < 7 * 24 * 3600
        ):
            version = entry.get("version")
            return version if isinstance(version, str) else None
        if not probe:
            return None
        version = command_version(command, timeout=1.5)
        with contextlib.suppress(OSError):
            atomic_write_json(
                cache_path,
                {"node": {"command": command, "version": version, "checked_at": now}},
            )
        return version

    def describe(self) -> dict:
        return {
            "root": str(self.root),
            "initialized": self.initialized,
            "model": self.config.model,
            "validator": self.config.validator,
            "validator_exists": self.validator_path.is_file(),
            "policy": str(self.policy_path),
            "sessions": str(self.sessions_dir),
            "opencode_config": str(self.opencode_config_path),
            "git": self.git_state(),
            "tools": self.tool_versions(probe=False),
        }


def resolve_chat_workspace(root: Path | None = None) -> tuple[Workspace, ConfigError | None]:
    """Resolve the chat workspace without writing to an unwritable cwd.

    `panda` is a global command: outside a corpus it still opens the cockpit,
    with a notice and the default policy, instead of exiting. When the current
    directory is not writable the user-level home workspace is used (and
    initialized on first use), so startup never fails on `PermissionError`.
    """

    if root is not None:
        return _resolve_explicit_root(root)

    cwd = Path.cwd().resolve()
    if find_workspace_root(cwd) is not None:
        try:
            return Workspace.load(cwd, require_init=True), None
        except ConfigError as exc:
            return Workspace.from_root(cwd), exc

    if is_writable_dir(cwd):
        return Workspace.from_root(cwd), ConfigError(
            "no Panda workspace found",
            hint="run `panda init` in the corpus root, or pass --root",
        )

    return _home_chat_workspace()


def _resolve_explicit_root(root: Path) -> tuple[Workspace, ConfigError | None]:
    resolved = root.resolve()
    if not resolved.is_dir():
        raise ConfigError(
            f"workspace root does not exist or is not a directory: {resolved}",
            hint="pass --root pointing at a corpus root or an initialized workspace",
        )
    if not is_writable_dir(resolved):
        raise ConfigError(
            f"workspace root is not writable: {resolved}",
            hint="pass --root with a writable directory, or set PANDA_HOME",
        )
    try:
        return Workspace.load(resolved, require_init=True), None
    except ConfigError as exc:
        return Workspace.from_root(resolved), exc


def _home_chat_workspace() -> tuple[Workspace, ConfigError | None]:
    from panda_core.init import initialize_workspace

    home = home_workspace_path()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(
            f"cannot create the Panda home workspace: {home}",
            hint="set PANDA_HOME to a writable directory, or pass --root",
        ) from exc
    if not is_writable_dir(home):
        raise ConfigError(
            f"the Panda home workspace is not writable: {home}",
            hint="set PANDA_HOME to a writable directory, or pass --root",
        )
    if not (home / CONFIG_DIR / CONFIG_FILE).is_file():
        try:
            initialize_workspace(home, allow_missing_validator=True, opencode=False)
        except ConfigError:
            raise
        except OSError as exc:
            raise ConfigError(
                f"cannot initialize the Panda home workspace: {home}",
                hint="set PANDA_HOME to a writable directory, or pass --root",
            ) from exc
    return Workspace.from_root(home, require_init=True), None
