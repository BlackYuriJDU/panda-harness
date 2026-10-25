"""OpenCode V2 integration: config generation, permission envelope, launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from panda_core.atomic import atomic_write_json
from panda_core.config import DEFAULT_MODEL
from panda_core.errors import ConfigError, DependencyError, ExecutionTimeout
from panda_core.process import (
    command_version,
    resolve_command_prefix,
    resolve_executable,
    split_command,
)
from panda_core.workspace import Workspace

README_INSTRUCTIONS = """# Panda harness instructions

You are operating inside a Panda evidence workspace. The harness is fail-closed:
a result that cannot be traced to a source line, a run, and a license does not
exist for publishing purposes.

## Non-negotiables

1. Every claim carries a `study_id` and a `track` (`public`, `simulated`, `real`).
2. Every number resolves to an `evidence_id`; every claim resolves to
   `evidence_ids` and `number_ids`.
3. Evidence carries `source_path:source_line` that resolves, a SHA-256 hash, a
   license, and a track.
4. A restricted or non-commercial source is metadata-only. Never copy its
   payload into the corpus or a release.
5. Never write outside the corpus/memory scopes without explicit human approval.
6. Never read or print `.env`, tokens, private keys, or credential stores.
7. Never push, publish, or rewrite git history. A human does that.
8. Run `panda validate` after changing corpus content. If the validator fails,
   stop and report the failing checks; do not rationalize a failure away.

## Workflow

- Read `corpus/README.md` and `corpus/studies/INDEX.md` before acting.
- Route work through a study card. If the study is not registered, create the
  card first; do not execute an unregistered analysis.
- Prefer the Panda MCP tools: `panda_workspace_info`, `panda_study_list`,
  `panda_study_show`, `panda_validate`, `panda_policy_check`,
  `panda_session_note`.
- A tool denial is final. Do not retry the same action through a different tool.
- Cite concrete locations in the form `path:line` in every summary.
- Separate observation, inference, and unknown. State what would falsify a claim.

## Public-face rules

`MEMORIA.md`, `memory/`, `colaboracao/`, `ARQUIVO_COMPLETO.md`, and
`_PRIVADO_DCJ_NAO_COMPARTILHAR/` never enter a public release. The release
manifest must list every included file and exclusion.

## Stop conditions

Ask the human when: a license is unclear, a source is restricted, a write would
land outside the allowed scopes, a claim would need a number that does not
resolve, or a requested action would publish or push.

If you cannot satisfy these rules, return a blocked result with the exact
missing evidence. Do not fabricate a substitute.
"""


@dataclass(frozen=True)
class ConfigWriteResult:
    path: Path
    created: bool
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


def default_panda_command() -> list[str]:
    override = os.environ.get("PANDA_BIN")
    if override:
        return split_command(override)
    if shutil.which("panda"):
        return ["panda"]
    return [sys.executable, "-m", "panda_cli"]


def build_permissions() -> list[dict[str, str]]:
    """OpenCode V2 permission envelope. Last matching rule wins."""

    rules: list[dict[str, str]] = [
        # Start from a deny-all baseline, then allow named capabilities.
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "read", "resource": "*", "effect": "allow"},
        {"action": "read", "resource": "*.env", "effect": "deny"},
        {"action": "read", "resource": "*.env.*", "effect": "deny"},
        {"action": "read", "resource": "*/.ssh/*", "effect": "deny"},
        {"action": "read", "resource": "*.pem", "effect": "deny"},
        {"action": "read", "resource": "*id_rsa*", "effect": "deny"},
        {"action": "read", "resource": "*id_ed25519*", "effect": "deny"},
        {"action": "glob", "resource": "*", "effect": "allow"},
        {"action": "grep", "resource": "*", "effect": "allow"},
        # Writes: only corpus/ and memory/; review scopes ask; the rest deny.
        {"action": "edit", "resource": "*", "effect": "deny"},
        {"action": "edit", "resource": "corpus/*", "effect": "allow"},
        {"action": "edit", "resource": "memory/*", "effect": "allow"},
        {"action": "edit", "resource": "corpus/releases/*", "effect": "ask"},
        {"action": "edit", "resource": "preprint/*", "effect": "ask"},
        {"action": "edit", "resource": "pipeline/*", "effect": "ask"},
        {"action": "edit", "resource": "tests/*", "effect": "ask"},
        {"action": "edit", "resource": "rules/*", "effect": "ask"},
        {"action": "edit", "resource": "research/*", "effect": "ask"},
        {"action": "edit", "resource": "colaboracao/*", "effect": "ask"},
        # Shell: ask by default, allow read-only commands, deny destructive ones.
        {"action": "shell", "resource": "*", "effect": "ask"},
        {"action": "shell", "resource": "git status *", "effect": "allow"},
        {"action": "shell", "resource": "git diff *", "effect": "allow"},
        {"action": "shell", "resource": "git log *", "effect": "allow"},
        {"action": "shell", "resource": "git show *", "effect": "allow"},
        {"action": "shell", "resource": "git branch *", "effect": "allow"},
        {"action": "shell", "resource": "node corpus/validation/validate.mjs *", "effect": "allow"},
        {"action": "shell", "resource": "panda validate *", "effect": "allow"},
        {"action": "shell", "resource": "git push *", "effect": "ask"},
        {"action": "shell", "resource": "git push *--force*", "effect": "deny"},
        {"action": "shell", "resource": "git push *-f*", "effect": "deny"},
        {"action": "shell", "resource": "git push *--mirror*", "effect": "deny"},
        {"action": "shell", "resource": "git reset *--hard*", "effect": "deny"},
        {"action": "shell", "resource": "git clean *-f*", "effect": "deny"},
        {"action": "shell", "resource": "rm -rf*", "effect": "deny"},
        {"action": "shell", "resource": "rm -fr*", "effect": "deny"},
        {"action": "shell", "resource": "Remove-Item *-Recurse*", "effect": "deny"},
        {"action": "shell", "resource": "Remove-Item *-Force*", "effect": "deny"},
        {"action": "shell", "resource": "*|*sh*", "effect": "deny"},
        {"action": "shell", "resource": "*|*bash*", "effect": "deny"},
        # External filesystem access is never implicit.
        {"action": "external_directory", "resource": "*", "effect": "deny"},
        {"action": "webfetch", "resource": "*", "effect": "ask"},
        {"action": "websearch", "resource": "*", "effect": "ask"},
        {"action": "question", "resource": "*", "effect": "allow"},
        {"action": "execute", "resource": "*", "effect": "allow"},
        {"action": "subagent", "resource": "*", "effect": "deny"},
        {"action": "skill", "resource": "*", "effect": "deny"},
        {"action": "opencode_list_mcp_resources", "resource": "panda", "effect": "allow"},
        {"action": "opencode_read_mcp_resource", "resource": "panda:*", "effect": "allow"},
        {"action": "panda_workspace_info", "resource": "*", "effect": "allow"},
        {"action": "panda_validate", "resource": "*", "effect": "allow"},
        {"action": "panda_policy_check", "resource": "*", "effect": "allow"},
        {"action": "panda_study_list", "resource": "*", "effect": "allow"},
        {"action": "panda_study_show", "resource": "*", "effect": "allow"},
        {"action": "panda_session_note", "resource": "*", "effect": "allow"},
    ]
    return rules


def build_opencode_config(
    workspace: Workspace,
    *,
    model: str | None = None,
    panda_command: Sequence[str] | None = None,
    include_panda_mcp: bool = True,
) -> dict[str, Any]:
    command = list(panda_command or default_panda_command())
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "model": model or workspace.config.model or DEFAULT_MODEL,
        "default_agent": workspace.config.opencode.agent,
        "instructions": [workspace.config.instructions],
        "permissions": build_permissions(),
    }
    if include_panda_mcp:
        config["mcp"] = {
            "servers": {
                "panda": {
                    "type": "local",
                    "command": [*command, "mcp", "serve", "--root", str(workspace.root)],
                    "cwd": str(workspace.root),
                    "environment": {
                        "PANDA_ROOT": str(workspace.root),
                        "PANDA_CONFIG": str(workspace.config_path),
                    },
                    "timeout": {
                        "startup": 30000,
                        "catalog": 30000,
                        "execution": 600000,
                    },
                }
            }
        }
    config["agents"] = {
        "panda": {
            "description": "Fail-closed evidence harness for this corpus",
            "mode": "primary",
            "system": (
                "You are Panda, a terminal harness for evidence-heavy research. "
                "Follow the workspace instructions. Every claim needs a study_id, "
                "track, and resolving evidence. Never push or publish. Ask when a "
                "license or write scope is unclear."
            ),
        }
    }
    return config


def resolve_config_target(workspace: Workspace) -> Path:
    """Pick the highest-precedence JSON config OpenCode will actually load."""

    configured = workspace.root / workspace.config.opencode.config_path
    root_json = workspace.root / "opencode.json"
    nested_json = workspace.root / ".opencode/opencode.json"
    root_jsonc = workspace.root / "opencode.jsonc"
    nested_jsonc = workspace.root / ".opencode/opencode.jsonc"

    if configured.suffix == ".jsonc":
        raise ConfigError(
            "the configured OpenCode path is JSONC; refusing to rewrite it",
            hint="point opencode.config_path at a .json file, or use --no-opencode",
        )
    if configured != root_json and (
        configured.is_file() or not (nested_json.is_file() or root_json.is_file())
    ):
        return configured
    if nested_json.is_file():
        return nested_json
    if root_json.is_file():
        return root_json
    if nested_jsonc.is_file() or root_jsonc.is_file():
        raise ConfigError(
            "found an OpenCode JSONC config; refusing to rewrite it",
            hint=(
                "convert opencode.jsonc to opencode.json, or run `panda init --no-opencode` "
                "and add the Panda MCP server manually"
            ),
        )
    return root_json


def _backup(path: Path, backups_dir: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backups_dir / stamp / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


def _unique_rules(rules: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop duplicate rules, keeping the last occurrence.

    Permission rules are evaluated in order with the last match winning, so a
    restrictive rule re-appended by the merge must stay reachable at the tail.
    Keeping the first occurrence would silently drop it.
    """

    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, str]] = []
    for rule in reversed(rules):
        key = (rule.get("action", ""), rule.get("resource", ""), rule.get("effect", ""))
        if key not in seen:
            seen.add(key)
            result.append(rule)
    result.reverse()
    return result


def merge_opencode_config(
    existing: dict[str, Any],
    generated: dict[str, Any],
    *,
    force_model: bool = False,
) -> tuple[dict[str, Any], list[str], list[str]]:
    merged = dict(existing)
    changes: list[str] = []
    warnings: list[str] = []

    if "$schema" not in merged:
        merged["$schema"] = generated["$schema"]
        changes.append("added $schema")

    if "model" not in merged:
        merged["model"] = generated["model"]
        changes.append(f"set model to {generated['model']}")
    elif force_model and merged["model"] != generated["model"]:
        changes.append(f"changed model from {merged['model']} to {generated['model']}")
        merged["model"] = generated["model"]
    elif merged["model"] != generated["model"]:
        warnings.append(
            f"kept existing model {merged['model']} (requested {generated['model']})"
        )

    if "default_agent" not in merged:
        merged["default_agent"] = generated["default_agent"]
        changes.append(f"set default_agent to {generated['default_agent']}")
    elif merged["default_agent"] != generated["default_agent"]:
        warnings.append(f"kept existing default_agent {merged['default_agent']}")

    existing_instructions = merged.get("instructions")
    if existing_instructions is None:
        instructions: list[str] = []
    elif isinstance(existing_instructions, str):
        instructions = [existing_instructions]
    elif isinstance(existing_instructions, list):
        instructions = [str(item) for item in existing_instructions]
    else:
        raise ConfigError("existing OpenCode 'instructions' must be a string or array")
    for instruction in generated.get("instructions", []):
        if instruction not in instructions:
            instructions.append(instruction)
            changes.append(f"added instruction {instruction}")
    merged["instructions"] = instructions

    existing_rules = merged.get("permissions")
    if existing_rules is None:
        existing_rules = []
    if not isinstance(existing_rules, list):
        raise ConfigError("existing OpenCode 'permissions' must be an array")
    generated_rules = generated.get("permissions", [])
    combined = _unique_rules([*existing_rules, *generated_rules])
    # Never weaken a user's explicit restriction: re-append deny/ask rules last.
    restrictive = [
        rule
        for rule in existing_rules
        if isinstance(rule, dict) and rule.get("effect") in {"deny", "ask"}
    ]
    merged["permissions"] = _unique_rules([*combined, *restrictive])
    if generated_rules:
        changes.append(f"applied {len(generated_rules)} Panda permission rules")

    mcp = merged.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ConfigError("existing OpenCode 'mcp' must be an object")
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        raise ConfigError("existing OpenCode 'mcp.servers' must be an object")
    if servers.get("panda") != generated.get("mcp", {}).get("servers", {}).get("panda"):
        servers["panda"] = generated["mcp"]["servers"]["panda"]
        changes.append("wired the Panda MCP server")

    agents = merged.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise ConfigError("existing OpenCode 'agents' must be an object")
    if "panda" not in agents:
        agents["panda"] = generated["agents"]["panda"]
        changes.append("added the Panda primary agent")
    elif agents["panda"] != generated["agents"]["panda"]:
        warnings.append("kept existing OpenCode agent 'panda'")

    return merged, changes, warnings


def write_opencode_config(
    workspace: Workspace,
    *,
    model: str | None = None,
    panda_command: Sequence[str] | None = None,
    force_model: bool = False,
) -> ConfigWriteResult:
    target = resolve_config_target(workspace)
    created = not target.is_file()
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"cannot merge invalid OpenCode JSON: {target} ({exc.msg})"
            ) from exc
        if not isinstance(existing, dict):
            raise ConfigError(f"OpenCode config must be a JSON object: {target}")
    else:
        existing = {}

    generated = build_opencode_config(
        workspace, model=model, panda_command=panda_command, include_panda_mcp=True
    )
    merged, changes, warnings = merge_opencode_config(
        existing, generated, force_model=force_model
    )
    if target.is_file():
        _backup(target, workspace.backups_dir)
    atomic_write_json(target, merged, mode=0o644)
    return ConfigWriteResult(
        path=target,
        created=created,
        changes=changes,
        warnings=warnings,
        payload=merged,
    )


def write_instructions(workspace: Workspace) -> Path:
    path = workspace.instructions_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(README_INSTRUCTIONS, encoding="utf-8", newline="\n")
    return path


def build_run_command(
    workspace: Workspace,
    task: str,
    *,
    model: str | None = None,
    agent: str | None = None,
    output_format: str | None = None,
    title: str | None = None,
    standalone: bool | None = None,
) -> list[str]:
    binary = split_command(workspace.config.opencode.binary)
    if not binary:
        raise ConfigError("opencode.binary is empty")
    command = [*binary, "run"]
    if standalone is None:
        standalone = workspace.config.opencode.standalone
    if standalone:
        command.append("--standalone")
    selected_model = model or workspace.config.model
    if selected_model:
        command.extend(["--model", selected_model])
    selected_agent = agent or workspace.config.opencode.agent
    if selected_agent:
        command.extend(["--agent", selected_agent])
    if output_format:
        if output_format not in {"default", "json"}:
            raise ConfigError(f"unsupported OpenCode output format: {output_format}")
        command.extend(["--format", output_format])
    if title:
        command.extend(["--title", title])
    command.extend(["--", task])
    return command


def opencode_available(workspace: Workspace) -> tuple[bool, str | None]:
    binary = split_command(workspace.config.opencode.binary)
    if not binary:
        return False, None
    resolved = resolve_executable(binary[0])
    if resolved is None:
        return False, None
    return True, command_version(binary)


def launch_opencode(
    workspace: Workspace,
    command: Sequence[str],
    *,
    session_id: str,
    session_path: Path,
    timeout: float | None = None,
) -> int:
    binary = split_command(workspace.config.opencode.binary)
    prefix = resolve_command_prefix(binary)
    if not binary or prefix is None:
        raise DependencyError(
            f"OpenCode executable not found: {binary[0] if binary else '<empty>'}",
            hint=(
                "install OpenCode v2 and ensure `opencode` is on PATH; "
                "on Windows the npm shim must point to a native executable"
            ),
        )
    resolved_command = list(command)
    if resolved_command[: len(binary)] == binary:
        resolved_command = [*prefix, *resolved_command[len(binary) :]]
    env = os.environ.copy()
    env.update(
        {
            "PANDA_ROOT": str(workspace.root),
            "PANDA_CONFIG": str(workspace.config_path),
            "PANDA_POLICY": str(workspace.policy_path),
            "PANDA_SESSION_ID": session_id,
            "PANDA_SESSION_FILE": str(session_path),
        }
    )
    try:
        completed = subprocess.run(
            resolved_command,
            cwd=str(workspace.root),
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutionTimeout(
            f"OpenCode run exceeded {timeout}s",
            hint="raise --timeout or narrow the task",
        ) from exc
    return completed.returncode


def config_status(workspace: Workspace) -> dict[str, Any]:
    path = workspace.opencode_config_path
    if not path.is_file():
        return {"exists": False, "path": str(path), "ok": False}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"exists": True, "path": str(path), "ok": False, "error": str(exc)}
    server = (
        config.get("mcp", {}).get("servers", {}).get("panda")
        if isinstance(config.get("mcp"), dict)
        else None
    )
    return {
        "exists": True,
        "path": str(path),
        "ok": bool(server),
        "model": config.get("model"),
        "default_agent": config.get("default_agent"),
        "panda_mcp": bool(server),
        "permission_rules": len(config.get("permissions", []))
        if isinstance(config.get("permissions"), list)
        else 0,
    }


def environment_status() -> dict[str, bool]:
    return {
        "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY")),
        "NO_COLOR": bool(os.environ.get("NO_COLOR")),
    }


def run_doctor_opencode(workspace: Workspace) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    available, version = opencode_available(workspace)
    checks.append(
        {
            "id": "opencode",
            "label": "OpenCode v2",
            "status": "ok" if available else "fail",
            "detail": version or "not found",
            "hint": "install OpenCode v2: https://opencode.ai/docs/",
            "required": True,
        }
    )
    status = config_status(workspace)
    checks.append(
        {
            "id": "opencode_config",
            "label": "OpenCode config + Panda MCP",
            "status": "ok" if status.get("ok") else "warn",
            "detail": status.get("path", ""),
            "hint": "run `panda init` to wire the Panda MCP server and permissions",
            "required": False,
        }
    )
    return checks


def dry_run_text(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    import shlex

    return shlex.join(list(command))
