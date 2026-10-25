"""Workspace discovery and accessors for `.panda/config.json`."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from panda_core.config import (
    DEFAULT_POLICY,
    DEFAULT_SESSIONS,
    DEFAULT_VALIDATOR,
    PandaConfig,
    default_config,
    load_config,
)
from panda_core.errors import ConfigError
from panda_core.process import command_version

CONFIG_DIR = ".panda"
CONFIG_FILE = "config.json"
POLICY_FILE = "policy.json"
SESSIONS_DIR = "sessions"
BACKUPS_DIR = "backups"
INSTRUCTIONS_FILE = "instructions.md"


def _candidates(start: Path) -> list[Path]:
    current = start.resolve()
    return [current, *current.parents]


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
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

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
        git_dir = self.root / ".git"
        if not git_dir.exists():
            return {"repo": False}
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {"repo": True, "commit": None, "branch": None, "dirty": None}
        return {
            "repo": True,
            "commit": commit.stdout.strip() or None,
            "branch": branch.stdout.strip() or None,
            "dirty": bool(status.stdout.strip()),
        }

    def tool_versions(self) -> dict:
        return {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "node": command_version(self.config.node),
        }

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
            "tools": self.tool_versions(),
        }
