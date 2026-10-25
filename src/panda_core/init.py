"""`panda init`: create the workspace contract.

The operation is idempotent and non-destructive by default. The OpenCode export
is legacy and opt-in (`opencode=True`); the runtime never depends on it.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from panda_core.atomic import atomic_write_text
from panda_core.config import DEFAULT_VALIDATOR, PandaConfig, default_config, load_config
from panda_core.errors import ConfigError
from panda_core.opencode import (
    ConfigWriteResult,
    build_opencode_config,
    default_panda_command,
    merge_opencode_config,
    resolve_config_target,
    write_instructions,
    write_opencode_config,
)
from panda_core.policy import write_default_policy
from panda_core.version import __version__
from panda_core.workspace import CONFIG_DIR, CONFIG_FILE, POLICY_FILE, Workspace


@dataclass
class InitResult:
    root: Path
    dry_run: bool = False
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    opencode_path: Path | None = None
    opencode_changes: list[str] = field(default_factory=list)
    config: PandaConfig | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "root": str(self.root),
            "dry_run": self.dry_run,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "warnings": self.warnings,
            "opencode_path": str(self.opencode_path) if self.opencode_path else None,
            "opencode_changes": self.opencode_changes,
            "config": self.config.to_jsonable() if self.config else None,
        }


def _backup(path: Path, backups_dir: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backups_dir / stamp / path.name
    counter = 1
    while destination.exists():
        destination = backups_dir / stamp / f"{path.stem}.{counter}{path.suffix}"
        counter += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


def _write_if_changed(
    path: Path,
    content: str,
    *,
    result: InitResult,
    force: bool,
    kind: str = "file",
) -> None:
    relative = str(path)
    if path.is_file() and not force:
        if path.read_text(encoding="utf-8") == content:
            result.unchanged.append(relative)
            return
        result.unchanged.append(relative)
        return
    if result.dry_run:
        (result.updated if path.is_file() else result.created).append(relative)
        return
    existed = path.is_file()
    atomic_write_text(path, content, mode=0o644)
    (result.updated if existed else result.created).append(relative)


def _write_json_if_changed(
    path: Path,
    payload: dict[str, Any],
    *,
    result: InitResult,
    force: bool,
    kind: str = "file",
) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _write_if_changed(path, content, result=result, force=force, kind=kind)


def _merge_gitignore(path: Path, lines: list[str], *, dry_run: bool) -> bool:
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    missing = [line for line in lines if line not in existing]
    if not missing:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join([*existing, *missing]).strip() + "\n"
        path.write_text(content, encoding="utf-8", newline="\n")
    return True


def initialize_workspace(
    root: Path,
    *,
    model: str | None = None,
    validator: str | None = None,
    allow_missing_validator: bool = False,
    force: bool = False,
    dry_run: bool = False,
    opencode: bool = False,
    panda_command: Sequence[str] | None = None,
    force_model: bool = False,
) -> InitResult:
    root = root.resolve()
    if not root.is_dir():
        raise ConfigError(f"workspace root does not exist or is not a directory: {root}")

    config_path = root / CONFIG_DIR / CONFIG_FILE
    existing_config: PandaConfig | None = None
    if config_path.is_file():
        if force:
            try:
                existing_config = load_config(config_path)
            except ConfigError:
                existing_config = None
        else:
            existing_config = load_config(config_path)

    config = (existing_config or default_config()).model_copy(deep=True)
    if model:
        config.model = model
    if validator:
        config.validator = validator
    config.validator = config.validator or DEFAULT_VALIDATOR
    config.created_with = __version__

    validator_path = root / config.validator
    if not validator_path.is_file() and not allow_missing_validator:
        raise ConfigError(
            f"validator not found: {validator_path}",
            hint=(
                "run `panda init --root <corpus-root>` from a corpus that contains "
                "corpus/validation/validate.mjs, or pass --allow-missing-validator"
            ),
        )

    if opencode:
        command = list(panda_command) if panda_command else list(config.opencode.panda_command)
        if not command or command == ["panda"]:
            command = default_panda_command()
        config.opencode.panda_command = command
    else:
        command = list(config.opencode.panda_command)

    result = InitResult(root=root, dry_run=dry_run, config=config)
    if dry_run:
        result.warnings.append("dry run: no files were written")
    if not os.environ.get(config.provider.api_key_env):
        result.warnings.append(
            f"{config.provider.api_key_env} is not set; model calls fail until it is exported"
        )
    if opencode:
        result.warnings.append(
            "legacy OpenCode export is enabled; the Panda runtime does not use opencode.json"
        )

    # Resolve the OpenCode target before writing anything so an unsupported
    # existing config fails without leaving partial state behind.
    if opencode:
        probe = Workspace(root=root, config=config, initialized=False)
        target = resolve_config_target(probe)
        try:
            config.opencode.config_path = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise ConfigError(f"OpenCode config target is outside the workspace: {target}") from exc
        result.opencode_path = target

    config_dir = root / CONFIG_DIR
    if not dry_run:
        config_dir.mkdir(parents=True, exist_ok=True)
        if config_path.is_file():
            _backup(config_path, root / CONFIG_DIR / "backups")

    _write_json_if_changed(
        config_path,
        config.to_jsonable(),
        result=result,
        force=True,
    )

    policy_path = root / CONFIG_DIR / POLICY_FILE
    policy_existed = policy_path.is_file()
    if force and policy_existed and not dry_run:
        _backup(policy_path, root / CONFIG_DIR / "backups")
    if not policy_existed or force:
        if not dry_run:
            write_default_policy(policy_path)
        (result.updated if policy_existed else result.created).append(str(policy_path))
    else:
        result.unchanged.append(str(policy_path))

    instructions_path = root / config.instructions
    instructions_existed = instructions_path.is_file()
    if force and instructions_existed and not dry_run:
        _backup(instructions_path, root / CONFIG_DIR / "backups")
    if not instructions_existed or force:
        if not dry_run:
            write_instructions(Workspace(root=root, config=config, initialized=True))
        (result.updated if instructions_existed else result.created).append(str(instructions_path))
    else:
        result.unchanged.append(str(instructions_path))

    sessions_dir = root / CONFIG_DIR / "sessions"
    if not dry_run:
        sessions_dir.mkdir(parents=True, exist_ok=True)
    gitignore = config_dir / ".gitignore"
    if _merge_gitignore(
        gitignore,
        ["sessions/", "backups/", "*.lock"],
        dry_run=dry_run,
    ):
        action = "would update" if dry_run else "updated"
        result.warnings.append(f"{action} {gitignore}")

    if opencode:
        if dry_run:
            try:
                existing = (
                    json.loads(result.opencode_path.read_text(encoding="utf-8"))
                    if result.opencode_path and result.opencode_path.is_file()
                    else {}
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"cannot read existing OpenCode config: {exc}") from exc
            generated = build_opencode_config(
                Workspace(root=root, config=config, initialized=True),
                model=config.model,
                panda_command=command,
            )
            _, changes, warnings = merge_opencode_config(
                existing, generated, force_model=force_model
            )
            result.opencode_changes = changes
            result.warnings.extend(warnings)
            if result.opencode_path and result.opencode_path.is_file():
                result.updated.append(str(result.opencode_path))
            elif result.opencode_path:
                result.created.append(str(result.opencode_path))
        else:
            write_result: ConfigWriteResult = write_opencode_config(
                Workspace(root=root, config=config, initialized=True),
                model=config.model,
                panda_command=command,
                force_model=force_model,
            )
            result.opencode_path = write_result.path
            result.opencode_changes = write_result.changes
            result.warnings.extend(write_result.warnings)
            (result.created if write_result.created else result.updated).append(
                str(write_result.path)
            )

    if not config.validator or not (root / config.validator).is_file():
        result.warnings.append(
            "validator is missing; `panda validate` will fail until it is installed"
        )
    return result
