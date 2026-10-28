"""JSON-schema tool registry shared by the cockpit, headless run, and MCP export.

Every mutating tool goes through `PolicyEngine` and path containment. Hard denies
return a blocked result instead of executing or raising, so the model can report
the block honestly. Corpus validation is delegated to the canonical Node
validator; nothing here reimplements those checks.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panda_core.atomic import atomic_write_text
from panda_core.errors import PandaError
from panda_core.policy import Decision, PolicyEngine
from panda_core.process import run_command
from panda_core.provider import status_snapshot
from panda_core.registry import find_study, load_registry, load_study_card, parse_frontmatter
from panda_core.session import SessionLog
from panda_core.validator import Validator
from panda_core.workspace import Workspace

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
}
MAX_READ_BYTES = 1_048_576
MAX_WRITE_BYTES = 2_097_152
PREVIEW_LIMIT = 6_000
DEFAULT_OUTPUT_LIMIT = 30_000


class ToolInputError(Exception):
    """Invalid tool arguments. Converted into a failed tool result."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool = True

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "read_only": self.read_only,
        }


@dataclass
class ToolResult:
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocked": self.blocked,
            "content": self.content,
            "data": self.data,
        }
@dataclass(frozen=True)
class ApprovalRequest:
    tool: str
    action: str
    resource: str
    rule_id: str | None
    risk: str
    reason: str
    preview: str = ""
    preview_kind: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "action": self.action,
            "resource": self.resource,
            "rule_id": self.rule_id,
            "risk": self.risk,
            "reason": self.reason,
            "preview": self.preview,
            "preview_kind": self.preview_kind,
        }


Approver = Callable[[ApprovalRequest], Awaitable[bool]]


@dataclass
class ToolContext:
    workspace: Workspace
    policy: PolicyEngine
    session: SessionLog | None = None
    active_study: str | None = None
    approve: Approver | None = None
    output_limit: int = DEFAULT_OUTPUT_LIMIT


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated {len(text) - limit} chars]"


_SECRET_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GEMINI_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
    }
)
_SECRET_ENV_PREFIXES = ("PANDA_", "OPENCODE_", "OPENROUTER_")
_SECRET_ENV_SUFFIXES = (
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
    "_CREDENTIALS",
)


def _scrubbed_env() -> dict[str, str]:
    """Child processes never inherit harness or provider credentials."""

    env: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in _SECRET_ENV_NAMES:
            continue
        if upper.startswith(_SECRET_ENV_PREFIXES):
            continue
        if upper.endswith(_SECRET_ENV_SUFFIXES):
            continue
        env[name] = value
    return env


def _read_exact(path: Path) -> str:
    """Read without newline translation so post-write checks are exact."""

    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def resolve_inside(root: Path, value: str) -> tuple[Path, str]:
    """Resolve a user path and refuse anything outside the workspace."""

    text = (value or "").strip()
    if not text:
        raise ToolInputError("path must not be empty")
    if "\x00" in text:
        raise ToolInputError("path contains a NUL byte")
    raw = Path(text)
    candidate = raw if raw.is_absolute() else root / raw
    try:
        resolved = candidate.resolve()
        relative = resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ToolInputError(f"path escapes the workspace: {value}") from exc
    return resolved, relative.as_posix()


def _diff_preview(relative: str, before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{relative}" if before else "/dev/null",
        tofile=f"b/{relative}",
        lineterm="",
    )
    return _cap("\n".join(diff) or "(no textual change)", PREVIEW_LIMIT)


def _blocked(tool: str, decision: Decision, detail: str) -> ToolResult:
    payload = {
        "ok": False,
        "blocked": True,
        "tool": tool,
        "action": decision.action,
        "resource": decision.resource,
        "effect": decision.effect,
        "risk": decision.risk,
        "rule": decision.rule_id,
        "reason": decision.reason or detail,
        "detail": detail,
    }
    return ToolResult(ok=False, blocked=True, content=_json(payload), data=payload)


async def _authorize(
    ctx: ToolContext,
    *,
    tool: str,
    action: str,
    resource: str,
    preview: str = "",
    preview_kind: str = "none",
) -> tuple[Decision | None, ToolResult | None]:
    decision = ctx.policy.decide(action, resource)
    if decision.effect == "allow":
        return decision, None
    if decision.effect in {"deny", "hard_deny"}:
        return decision, _blocked(tool, decision, "policy denies this action")
    if ctx.approve is None:
        return decision, _blocked(
            tool, decision, "approval is required and no interactive approver is available"
        )
    request = ApprovalRequest(
        tool=tool,
        action=action,
        resource=resource,
        rule_id=decision.rule_id,
        risk=decision.risk,
        reason=decision.reason,
        preview=preview,
        preview_kind=preview_kind,
    )
    try:
        approved = bool(await ctx.approve(request))
    except Exception:  # noqa: BLE001 - approval failure must deny, never allow
        approved = False
    if not approved:
        return decision, _blocked(tool, decision, "approval denied by the operator")
    return decision, None


def _fail(tool: str, message: str, *, data: dict[str, Any] | None = None) -> ToolResult:
    payload = {"ok": False, "tool": tool, "error": message, **(data or {})}
    return ToolResult(ok=False, content=_json(payload), data=payload)


def _ok(tool: str, payload: dict[str, Any], *, text: str | None = None) -> ToolResult:
    body = {"ok": True, "tool": tool, **payload}
    return ToolResult(ok=True, content=text if text is not None else _json(body), data=body)

def _string_arg(args: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = args.get(key, default)
    if not isinstance(value, str):
        raise ToolInputError(f"{key} must be a string")
    return value


def _int_arg(
    args: dict[str, Any], key: str, *, default: int, minimum: int, maximum: int
) -> int:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolInputError(f"{key} must be an integer")
    return max(minimum, min(maximum, int(value)))


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _record_id(record: dict[str, Any], keys: Sequence[str], fallback: str = "") -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return fallback


def _id_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ToolInputError(f"file not found: {path.name}")
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ToolInputError(f"invalid JSON at {path.name}:{number}: {exc.msg}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def _numbers_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ToolInputError(f"file not found: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolInputError(f"invalid JSON in {path.name}: {exc.msg}") from exc
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("numbers", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        records: list[dict[str, Any]] = []
        for key, value in payload.items():
            if isinstance(value, dict):
                record = dict(value)
                record.setdefault("number_id", key)
                records.append(record)
        return records
    raise ToolInputError(f"{path.name} must contain a list or object of numbers")


def _iter_files(root: Path, pattern: str, limit: int) -> list[Path]:
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ToolInputError("pattern must be relative and stay inside the workspace")
    found: list[Path] = []
    for candidate in sorted(root.glob(pattern)):
        try:
            relative = candidate.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        found.append(candidate)
        if len(found) >= limit:
            break
    return found


class ToolRegistry:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[ToolResult]]] = {
            "workspace_info": self._workspace_info,
            "read_file": self._read_file,
            "list_files": self._list_files,
            "grep": self._grep,
            "study_list": self._study_list,
            "study_show": self._study_show,
            "validate": self._validate,
            "policy_check": self._policy_check,
            "session_note": self._session_note,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "run_shell": self._run_shell,
            "evidence_list": self._evidence_list,
            "evidence_show": self._evidence_show,
            "claim_list": self._claim_list,
            "claim_show": self._claim_show,
            "number_list": self._number_list,
            "number_show": self._number_show,
        }
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in self._build_specs()}
    # ---- contract ----
    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs)

    def to_openai(self) -> list[dict[str, Any]]:
        return [spec.to_openai() for spec in self._specs.values()]

    def describe(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._specs.values()]

    async def call(self, name: str, arguments: dict[str, Any] | str | None) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            return _fail(name, f"unknown tool: {name}", data={"available": list(self._specs)})
        if arguments is None:
            args: dict[str, Any] = {}
        elif isinstance(arguments, str):
            text = arguments.strip()
            if not text:
                args = {}
            else:
                try:
                    value = json.loads(text)
                except json.JSONDecodeError as exc:
                    return _fail(name, f"arguments are not valid JSON: {exc.msg}")
                if not isinstance(value, dict):
                    return _fail(name, "arguments must be a JSON object")
                args = value
        elif isinstance(arguments, dict):
            args = arguments
        else:
            return _fail(name, "arguments must be an object or JSON object string")

        required = spec.parameters.get("required", [])
        missing = [key for key in required if key not in args]
        if missing:
            return _fail(name, f"missing required argument(s): {', '.join(missing)}")
        handler = self._handlers[name]
        try:
            return await handler(args)
        except ToolInputError as exc:
            return _fail(name, str(exc))
        except PandaError as exc:
            return _fail(name, exc.message, data={"code": exc.code, **(exc.details or {})})
        except OSError as exc:
            return _fail(name, f"filesystem error: {exc.strerror or exc}")
        except (ValueError, TypeError) as exc:
            return _fail(name, f"invalid arguments: {exc}")

    # ---- read tools ----
    async def _workspace_info(self, args: dict[str, Any]) -> ToolResult:
        workspace = self.ctx.workspace
        info = workspace.describe()
        info.pop("opencode_config", None)
        info["provider"] = status_snapshot()
        info["provider"]["model"] = workspace.config.model
        info["active_study"] = self.ctx.active_study
        info["session_id"] = self.ctx.session.session_id if self.ctx.session else None
        try:
            studies = load_registry(workspace.root)
            info["studies"] = {"count": len(studies), "ids": [study.id for study in studies]}
        except PandaError as exc:
            info["studies"] = {"count": 0, "error": exc.message}
        return _ok("workspace_info", info)

    async def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path_value = _string_arg(args, "path")
        start = _int_arg(args, "start_line", default=1, minimum=1, maximum=1_000_000)
        limit = _int_arg(args, "max_lines", default=400, minimum=1, maximum=2_000)
        path, relative = resolve_inside(self.ctx.workspace.root, path_value)
        decision, blocked = await _authorize(
            self.ctx, tool="read_file", action="read", resource=relative
        )
        if blocked:
            return blocked
        if not path.is_file():
            raise ToolInputError(f"file not found: {relative}")
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolInputError(
                f"file is larger than {MAX_READ_BYTES} bytes; use grep or a narrower tool"
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        end = min(total, start - 1 + limit)
        numbered = "\n".join(
            f"{number:>5}  {lines[number - 1]}" for number in range(start, end + 1)
        )
        header = f"{relative}:{start}-{end} of {total} lines"
        body = f"{header}\n{numbered}" if numbered else f"{header}\n(empty)"
        return _ok(
            "read_file",
            {
                "path": relative,
                "total_lines": total,
                "start_line": start,
                "end_line": end,
                "truncated": end < total,
                "rule": decision.rule_id if decision else None,
            },
            text=body,
        )
    async def _list_files(self, args: dict[str, Any]) -> ToolResult:
        pattern = _string_arg(args, "pattern", default="*") or "*"
        limit = _int_arg(args, "limit", default=200, minimum=1, maximum=2_000)
        decision, blocked = await _authorize(
            self.ctx, tool="list_files", action="read", resource="."
        )
        if blocked:
            return blocked
        found = _iter_files(self.ctx.workspace.root, pattern, limit)
        entries: list[str] = []
        for candidate in found:
            relative = candidate.relative_to(self.ctx.workspace.root.resolve()).as_posix()
            entries.append(relative + ("/" if candidate.is_dir() else ""))
        return _ok(
            "list_files",
            {
                "pattern": pattern,
                "count": len(entries),
                "entries": entries,
                "rule": decision.rule_id if decision else None,
            },
        )

    async def _grep(self, args: dict[str, Any]) -> ToolResult:
        pattern = _string_arg(args, "pattern")
        glob = _string_arg(args, "glob", default="**/*") or "**/*"
        limit = _int_arg(args, "max_matches", default=100, minimum=1, maximum=1_000)
        case_sensitive = bool(args.get("case_sensitive", False))
        fixed = bool(args.get("fixed_strings", False))
        if not pattern:
            raise ToolInputError("pattern must not be empty")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(re.escape(pattern) if fixed else pattern, flags)
        except re.error as exc:
            raise ToolInputError(f"invalid regular expression: {exc}") from exc
        _, blocked = await _authorize(self.ctx, tool="grep", action="read", resource=".")
        if blocked:
            return blocked
        matches: list[str] = []
        files_scanned = 0
        truncated = False
        root = self.ctx.workspace.root.resolve()
        for candidate in _iter_files(self.ctx.workspace.root, glob, 5_000):
            if not candidate.is_file() or candidate.stat().st_size > MAX_READ_BYTES:
                continue
            files_scanned += 1
            try:
                raw = candidate.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:2048]:
                continue
            text = raw.decode("utf-8", "replace")
            relative = candidate.relative_to(root).as_posix()
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{relative}:{number}: {line[:300]}")
                    if len(matches) >= limit:
                        truncated = True
                        break
            if truncated:
                break
        return _ok(
            "grep",
            {
                "pattern": pattern,
                "glob": glob,
                "files_scanned": files_scanned,
                "count": len(matches),
                "matches": matches,
                "truncated": truncated,
            },
        )

    async def _study_list(self, args: dict[str, Any]) -> ToolResult:
        track = args.get("track")
        status = args.get("status")
        studies = [study.to_dict() for study in load_registry(self.ctx.workspace.root)]
        if isinstance(track, str) and track:
            studies = [study for study in studies if study["track"] == track]
        if isinstance(status, str) and status:
            studies = [study for study in studies if study["status"] == status]
        return _ok("study_list", {"studies": studies, "count": len(studies)})

    async def _study_show(self, args: dict[str, Any]) -> ToolResult:
        study_id = _string_arg(args, "study_id")
        study = find_study(self.ctx.workspace.root, study_id)
        card = load_study_card(study)
        payload = study.to_dict()
        payload["frontmatter"] = card["frontmatter"]
        payload["body"] = _cap(card["body"], 8_000)
        return _ok("study_show", payload)

    async def _validate(self, args: dict[str, Any]) -> ToolResult:
        release = bool(args.get("release", False))
        workspace = self.ctx.workspace
        report = await asyncio.to_thread(
            lambda: Validator(
                workspace.root,
                script=workspace.validator_path,
                node=workspace.config.node,
                min_major=workspace.config.node_min_major,
                timeout=workspace.config.timeouts.validate_seconds,
            ).run(release=release)
        )
        payload = report.to_dict()
        return ToolResult(
            ok=report.ok,
            content=_json({"ok": report.ok, "tool": "validate", **payload}),
            data=payload,
        )

    async def _policy_check(self, args: dict[str, Any]) -> ToolResult:
        action = _string_arg(args, "action")
        resource = _string_arg(args, "resource")
        normalized = resource
        if action in {"read", "write", "edit", "delete"} and resource:
            try:
                _, normalized = resolve_inside(self.ctx.workspace.root, resource)
            except ToolInputError:
                normalized = resource
        decision = self.ctx.policy.decide(action, normalized)
        return _ok("policy_check", decision.to_dict())

    async def _session_note(self, args: dict[str, Any]) -> ToolResult:
        message = _string_arg(args, "message").strip()
        if not message:
            raise ToolInputError("message must not be empty")
        if self.ctx.session is None:
            return _fail("session_note", "no active session ledger")
        text = _cap(message, 4_000)
        self.ctx.session.append("note", {"message": text, "tool": "session_note"})
        return _ok(
            "session_note",
            {"recorded": True, "session_id": self.ctx.session.session_id, "chars": len(text)},
        )
    # ---- write tools ----
    async def _write_file(self, args: dict[str, Any]) -> ToolResult:
        path_value = _string_arg(args, "path")
        content = _string_arg(args, "content")
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolInputError(f"content exceeds {MAX_WRITE_BYTES} bytes")
        path, relative = resolve_inside(self.ctx.workspace.root, path_value)
        if path.is_dir():
            raise ToolInputError(f"path is a directory: {relative}")
        before = ""
        if path.is_file():
            try:
                before = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise ToolInputError(f"cannot read existing file: {exc}") from exc
        preview = _diff_preview(relative, before, content)
        _, blocked = await _authorize(
            self.ctx,
            tool="write_file",
            action="write",
            resource=relative,
            preview=preview,
            preview_kind="diff",
        )
        if blocked:
            return blocked
        changed = before != content
        if changed:
            atomic_write_text(path, content, mode=0o644)
            try:
                written = _read_exact(path)
            except OSError as exc:
                return _fail(
                    "write_file", f"post-write verification failed: {exc}"
                )
            if written != content:
                return _fail(
                    "write_file",
                    "post-write verification failed: content differs after write",
                )
        return _ok(
            "write_file",
            {
                "path": relative,
                "bytes": len(content.encode("utf-8")),
                "changed": changed,
                "created": not before and changed,
            },
        )

    async def _edit_file(self, args: dict[str, Any]) -> ToolResult:
        path_value = _string_arg(args, "path")
        old = _string_arg(args, "old_string")
        new = _string_arg(args, "new_string")
        replace_all = bool(args.get("replace_all", False))
        if not old:
            raise ToolInputError("old_string must not be empty")
        path, relative = resolve_inside(self.ctx.workspace.root, path_value)
        if not path.is_file():
            raise ToolInputError(f"file not found: {relative}")
        if path.stat().st_size > MAX_WRITE_BYTES:
            raise ToolInputError(f"file exceeds {MAX_WRITE_BYTES} bytes")
        before = path.read_text(encoding="utf-8", errors="replace")
        count = before.count(old)
        if count == 0:
            raise ToolInputError("old_string was not found; read the file before editing")
        if count > 1 and not replace_all:
            raise ToolInputError(
                f"old_string appears {count} times; provide more context or set replace_all"
            )
        after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
        preview = _diff_preview(relative, before, after)
        _, blocked = await _authorize(
            self.ctx,
            tool="edit_file",
            action="edit",
            resource=relative,
            preview=preview,
            preview_kind="diff",
        )
        if blocked:
            return blocked
        atomic_write_text(path, after, mode=0o644)
        try:
            written = _read_exact(path)
        except OSError as exc:
            return _fail("edit_file", f"post-write verification failed: {exc}")
        if written != after:
            return _fail(
                "edit_file",
                "post-write verification failed: content differs after edit",
            )
        return _ok(
            "edit_file",
            {
                "path": relative,
                "replacements": count if replace_all else 1,
                "bytes": len(after.encode("utf-8")),
            },
        )

    async def _run_shell(self, args: dict[str, Any]) -> ToolResult:
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(part, str) for part in argv
        ):
            raise ToolInputError("argv must be a non-empty array of strings (no shell string)")
        if any("\x00" in part for part in argv):
            raise ToolInputError("argv contains a NUL byte")
        timeout = _int_arg(args, "timeout_seconds", default=60, minimum=1, maximum=600)
        cwd_value = _string_arg(args, "cwd", default=".")
        cwd, cwd_relative = resolve_inside(self.ctx.workspace.root, cwd_value or ".")
        if not cwd.is_dir():
            raise ToolInputError(f"cwd is not a directory: {cwd_relative}")
        command_text = " ".join(argv)
        _, blocked = await _authorize(
            self.ctx,
            tool="run_shell",
            action="shell",
            resource=command_text,
            preview=command_text,
            preview_kind="command",
        )
        if blocked:
            return blocked
        result = await asyncio.to_thread(
            run_command, argv, cwd=cwd, timeout=timeout, env=_scrubbed_env()
        )
        payload = {
            "argv": argv,
            "cwd": cwd_relative,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "duration_ms": result.duration_ms,
            "stdout": _cap(result.stdout, self.ctx.output_limit // 2),
            "stderr": _cap(result.stderr, self.ctx.output_limit // 2),
        }
        return ToolResult(
            ok=result.ok,
            content=_json({"ok": result.ok, "tool": "run_shell", **payload}),
            data=payload,
        )
    # ---- biomedical read-only tools ----
    def _evidence_path(self) -> tuple[Path, str]:
        return resolve_inside(self.ctx.workspace.root, "corpus/evidence/evidence.jsonl")

    async def _evidence_list(self, args: dict[str, Any]) -> ToolResult:
        path, relative = self._evidence_path()
        _, blocked = await _authorize(
            self.ctx, tool="evidence_list", action="read", resource=relative
        )
        if blocked:
            return blocked
        records = _read_jsonl(path)
        limit = _int_arg(args, "limit", default=100, minimum=1, maximum=1_000)
        study_id = args.get("study_id")
        summaries = []
        for record in records:
            if (
                isinstance(study_id, str)
                and study_id
                and str(record.get("study_id", "")) != study_id
            ):
                continue
            summaries.append(
                {
                    "evidence_id": _record_id(record, ("evidence_id", "id", "evidenceId")),
                    "study_id": record.get("study_id"),
                    "track": record.get("track"),
                    "source_path": record.get("source_path"),
                    "source_line": record.get("source_line"),
                    "license": record.get("license"),
                }
            )
        return _ok(
            "evidence_list",
            {
                "path": relative,
                "count": len(summaries[:limit]),
                "total": len(records),
                "truncated": len(summaries) > limit,
                "evidence": summaries[:limit],
            },
        )

    async def _evidence_show(self, args: dict[str, Any]) -> ToolResult:
        evidence_id = _string_arg(args, "evidence_id")
        path, relative = self._evidence_path()
        _, blocked = await _authorize(
            self.ctx, tool="evidence_show", action="read", resource=relative
        )
        if blocked:
            return blocked
        for record in _read_jsonl(path):
            if _record_id(record, ("evidence_id", "id", "evidenceId")) == evidence_id:
                return _ok("evidence_show", {"path": relative, "evidence": record})
        raise ToolInputError(f"evidence not found: {evidence_id}")

    def _claim_records(self) -> list[dict[str, Any]]:
        directory = self.ctx.workspace.root / "corpus/claims"
        if not directory.is_dir():
            raise ToolInputError("claims directory not found: corpus/claims")
        records: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.md")):
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                frontmatter, body = parse_frontmatter(text)
            except PandaError as exc:
                raise ToolInputError(f"{path.name}: {exc.message}") from exc
            record = dict(frontmatter)
            record.setdefault("claim_id", path.stem)
            record["file"] = path.name
            record["body"] = body
            records.append(record)
        return records

    async def _claim_list(self, args: dict[str, Any]) -> ToolResult:
        _, blocked = await _authorize(
            self.ctx, tool="claim_list", action="read", resource="corpus/claims"
        )
        if blocked:
            return blocked
        records = self._claim_records()
        limit = _int_arg(args, "limit", default=100, minimum=1, maximum=1_000)
        summaries = [
            {
                "claim_id": _record_id(record, ("claim_id", "id")),
                "file": record.get("file"),
                "study_id": record.get("study_id"),
                "track": record.get("track"),
                "status": record.get("status"),
                "evidence_ids": _id_list(record.get("evidence_ids")),
                "number_ids": _id_list(record.get("number_ids")),
            }
            for record in records
        ]
        return _ok(
            "claim_list",
            {
                "count": len(summaries[:limit]),
                "total": len(summaries),
                "truncated": len(summaries) > limit,
                "claims": summaries[:limit],
            },
        )

    async def _claim_show(self, args: dict[str, Any]) -> ToolResult:
        claim_id = _string_arg(args, "claim_id")
        _, blocked = await _authorize(
            self.ctx, tool="claim_show", action="read", resource="corpus/claims"
        )
        if blocked:
            return blocked
        for record in self._claim_records():
            if _record_id(record, ("claim_id", "id")) == claim_id:
                payload = dict(record)
                payload["body"] = _cap(str(payload.get("body", "")), 8_000)
                return _ok("claim_show", {"claim": payload})
        raise ToolInputError(f"claim not found: {claim_id}")

    def _numbers_path(self) -> tuple[Path, str]:
        return resolve_inside(self.ctx.workspace.root, "corpus/numbers.json")

    async def _number_list(self, args: dict[str, Any]) -> ToolResult:
        path, relative = self._numbers_path()
        _, blocked = await _authorize(
            self.ctx, tool="number_list", action="read", resource=relative
        )
        if blocked:
            return blocked
        records = _numbers_records(path)
        limit = _int_arg(args, "limit", default=100, minimum=1, maximum=1_000)
        summaries = []
        for record in records:
            value = record.get("value")
            summaries.append(
                {
                    "number_id": _record_id(record, ("number_id", "id", "number")),
                    "value": value,
                    "numeric": _number(value),
                    "unit": record.get("unit"),
                    "evidence_id": record.get("evidence_id"),
                    "study_id": record.get("study_id"),
                }
            )
        return _ok(
            "number_list",
            {
                "path": relative,
                "count": len(summaries[:limit]),
                "total": len(summaries),
                "truncated": len(summaries) > limit,
                "numbers": summaries[:limit],
            },
        )

    async def _number_show(self, args: dict[str, Any]) -> ToolResult:
        number_id = _string_arg(args, "number_id")
        path, relative = self._numbers_path()
        _, blocked = await _authorize(
            self.ctx, tool="number_show", action="read", resource=relative
        )
        if blocked:
            return blocked
        for record in _numbers_records(path):
            if _record_id(record, ("number_id", "id", "number")) == number_id:
                return _ok("number_show", {"path": relative, "number": record})
        raise ToolInputError(f"number not found: {number_id}")
    # ---- specs ----
    def _build_specs(self) -> list[ToolSpec]:
        obj: dict[str, Any] = {"type": "object"}
        return [
            ToolSpec(
                "workspace_info",
                "Workspace root, provider, model, validator, policy, studies, session state.",
                {**obj, "properties": {}, "additionalProperties": False},
            ),
            ToolSpec(
                "read_file",
                "Read a UTF-8 text file inside the workspace, with line numbers for citation.",
                {
                    **obj,
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative path."},
                        "start_line": {"type": "integer", "minimum": 1, "default": 1},
                        "max_lines": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 2000,
                            "default": 400,
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "list_files",
                "List workspace files matching a glob, skipping VCS and dependency directories.",
                {
                    **obj,
                    "properties": {
                        "pattern": {"type": "string", "default": "*"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                    },
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "grep",
                "Search text files with a regular expression; returns path:line: text.",
                {
                    **obj,
                    "properties": {
                        "pattern": {"type": "string"},
                        "glob": {"type": "string", "default": "**/*"},
                        "max_matches": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "default": 100,
                        },
                        "case_sensitive": {"type": "boolean", "default": False},
                        "fixed_strings": {"type": "boolean", "default": False},
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "study_list",
                "List registered studies from corpus/studies/INDEX.md.",
                {
                    **obj,
                    "properties": {
                        "track": {"type": "string", "enum": ["public", "simulated", "real"]},
                        "status": {"type": "string", "enum": ["planned", "active", "done"]},
                    },
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "study_show",
                "Show one registered study card, its frontmatter, and its body.",
                {
                    **obj,
                    "properties": {"study_id": {"type": "string"}},
                    "required": ["study_id"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "validate",
                "Run the canonical corpus validator and return its JSON report.",
                {
                    **obj,
                    "properties": {"release": {"type": "boolean", "default": False}},
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "policy_check",
                "Evaluate the fail-closed policy for one action/resource pair.",
                {
                    **obj,
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "read",
                                "write",
                                "edit",
                                "delete",
                                "shell",
                                "network",
                                "git",
                                "release",
                                "spend",
                                "tool",
                            ],
                        },
                        "resource": {"type": "string"},
                    },
                    "required": ["action", "resource"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "session_note",
                "Append a durable observation to the hash-chained session ledger.",
                {
                    **obj,
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "write_file",
                "Write a workspace file (create or replace) after policy approval.",
                {
                    **obj,
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                read_only=False,
            ),
            ToolSpec(
                "edit_file",
                "Replace one exact string (or every occurrence) in a workspace file.",
                {
                    **obj,
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean", "default": False},
                    },
                    "required": ["path", "old_string", "new_string"],
                    "additionalProperties": False,
                },
                read_only=False,
            ),
            ToolSpec(
                "run_shell",
                "Run a bounded argv command without a shell, from a workspace directory.",
                {
                    **obj,
                    "properties": {
                        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "cwd": {"type": "string", "default": "."},
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 600,
                            "default": 60,
                        },
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
                read_only=False,
            ),
            ToolSpec(
                "evidence_list",
                "List evidence records from corpus/evidence/evidence.jsonl.",
                {
                    **obj,
                    "properties": {
                        "study_id": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "default": 100,
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "evidence_show",
                "Show one evidence record by evidence_id.",
                {
                    **obj,
                    "properties": {"evidence_id": {"type": "string"}},
                    "required": ["evidence_id"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "claim_list",
                "List claims from corpus/claims/*.md frontmatter.",
                {
                    **obj,
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "default": 100,
                        }
                    },
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "claim_show",
                "Show one claim and its body by claim_id.",
                {
                    **obj,
                    "properties": {"claim_id": {"type": "string"}},
                    "required": ["claim_id"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "number_list",
                "List numbers from corpus/numbers.json with their evidence link.",
                {
                    **obj,
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "default": 100,
                        }
                    },
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                "number_show",
                "Show one number record by number_id.",
                {
                    **obj,
                    "properties": {"number_id": {"type": "string"}},
                    "required": ["number_id"],
                    "additionalProperties": False,
                },
            ),
        ]


__all__ = [
    "ApprovalRequest",
    "Approver",
    "ToolContext",
    "ToolInputError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "resolve_inside",
]