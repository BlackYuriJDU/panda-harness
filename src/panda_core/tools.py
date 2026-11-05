"""JSON-schema tool registry shared by the cockpit, headless run, and MCP export.

Every mutating tool goes through `PolicyEngine` and path containment. Hard denies
return a blocked result instead of executing or raising, so the model can report
the block honestly. Corpus validation is delegated to the canonical Node
validator; nothing here reimplements those checks.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from panda_core import gitsafe
from panda_core.atomic import atomic_write_bytes, atomic_write_text
from panda_core.errors import ConfigError, PandaError
from panda_core.policy import Decision, PolicyEngine
from panda_core.process import run_command, scrubbed_env
from panda_core.provider import status_snapshot
from panda_core.redact import REDACTED, redact_text
from panda_core.registry import find_study, load_registry, load_study_card, parse_frontmatter
from panda_core.safe_read import ensure_plain_path
from panda_core.session import SessionLog
from panda_core.treehash import file_sha256
from panda_core.validator import Validator, validator_sha256
from panda_core.workspace import Workspace

GREP_TIMEOUT_SECONDS = 10.0
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
# A diff is shown whole up to this size; past it the action cannot be approved (R2-125).
PREVIEW_LIMIT = 200_000
DEFAULT_OUTPUT_LIMIT = 30_000
# R2-408: one result may use at most this share of the context window (at ~4
# characters per token, the estimate the agent trims against), and never more
# than RESULT_LIMIT_MAX characters. Longer lines are cut so one line cannot.
RESULT_WINDOW_SHARE = 0.2
RESULT_LIMIT_MAX = 200_000
RESULT_LIMIT_MIN = 8_000
LINE_LIMIT = 2_000


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
    # From the resolved decision, never from a rule id (R2-500): what --auto reads.
    effect_class: str = ""
    # The consent card (R2-229, R2-117): exactly what runs, where, under which rule.
    argv: tuple[str, ...] = ()
    cwd: str = ""
    rule_source: str = ""
    payload_sha256: str = ""
    preview_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_class": self.effect_class or self.action,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "rule_source": self.rule_source,
            "payload_sha256": self.payload_sha256,
            "preview_complete": self.preview_complete,
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
    result_limit: int | None = None
    # Who answered the last approval request: human, auto or policy (set by the agent).
    decided_by: str | None = None

    def max_result_chars(self) -> int:
        """Characters one tool result may carry to the model (R2-408)."""

        if self.result_limit is not None:
            return max(1, self.result_limit)
        window = self.workspace.config.provider.context_window
        share = int(window * 4 * RESULT_WINDOW_SHARE)
        return max(RESULT_LIMIT_MIN, min(RESULT_LIMIT_MAX, share))


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated {len(text) - limit} chars]"


def _scrubbed_env() -> dict[str, str]:
    """Child processes never inherit harness or provider credentials."""

    return scrubbed_env()


def _line_ending(text: str) -> str:
    """The single line ending of `text`; mixed endings are refused (EDIT-001)."""

    crlf = text.count("\r\n")
    lone_lf = text.count("\n") - crlf
    lone_cr = text.count("\r") - crlf
    kinds = [kind for kind, n in (("\r\n", crlf), ("\n", lone_lf), ("\r", lone_cr)) if n]
    if len(kinds) > 1:
        raise ToolInputError(
            "file has mixed line endings; edit_file will not guess which to keep "
            "(use write_file with the full content)"
        )
    return kinds[0] if kinds else "\n"


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


def _blocked(
    tool: str, decision: Decision, detail: str, *, next_step: str | None = None
) -> ToolResult:
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
        "next_step": next_step or _denied_next_step(decision, "built-in"),
    }
    return ToolResult(ok=False, blocked=True, content=_json(payload), data=payload)


def _denied_next_step(decision: Decision, source: str) -> str:
    """What the model should do after a policy denial (R2-412)."""

    where = {
        "workspace": "the workspace policy (.panda/policy.json)",
        "classifier": "Panda's command classifier",
    }.get(source, "Panda's built-in policy")
    return (
        f"Do not retry this by any other route (another tool, a wrapper, a different path). "
        f"Tell the user that rule {decision.rule_id or 'default'} in {where} blocks it, and "
        "ask how they want to proceed."
    )


# R2-412: a blocked approval tells the model what kind of stop it is.
_APPROVAL_NEXT_STEP = {
    "policy": (
        "approval is required and no interactive approver is available",
        "Do not try another route. Tell the user this action needs their approval: they can "
        "run it in an interactive session (panda chat) or do it themselves.",
    ),
    "auto": (
        "--auto does not approve this kind of action",
        "Do not try another route. --auto only approves workspace edits and local checks; "
        "tell the user this action needs a person's approval.",
    ),
    "human": (
        "approval denied by the operator",
        "The user said no. Do not retry the same action; ask the user why before trying "
        "an alternative.",
    ),
}


async def _authorize(
    ctx: ToolContext,
    *,
    tool: str,
    action: str,
    resource: str,
    preview: str = "",
    preview_kind: str = "none",
    argv: Sequence[str] | None = None,
    cwd: str = "",
    payload_sha256: str = "",
) -> tuple[Decision | None, ToolResult | None]:
    decision = (
        ctx.policy.decide_shell(argv) if argv is not None else ctx.policy.decide(action, resource)
    )
    if decision.effect == "allow":
        return decision, None
    if decision.effect in {"deny", "hard_deny"}:
        source = ctx.policy.rule_source(decision.rule_id)
        return decision, _blocked(
            tool,
            decision,
            "policy denies this action",
            next_step=_denied_next_step(decision, source),
        )
    return decision, await _ask(
        ctx,
        decision,
        tool=tool,
        action=action,
        resource=resource,
        preview=preview,
        preview_kind=preview_kind,
        argv=tuple(argv or ()),
        cwd=cwd,
        payload_sha256=payload_sha256,
    )


async def _ask(
    ctx: ToolContext,
    decision: Decision,
    *,
    tool: str,
    action: str,
    resource: str,
    preview: str,
    preview_kind: str,
    argv: tuple[str, ...] = (),
    cwd: str = "",
    payload_sha256: str = "",
) -> ToolResult | None:
    """Ask the approver; `None` means approved, a result means blocked."""

    if ctx.approve is None:
        detail, step = _APPROVAL_NEXT_STEP["policy"]
        return _blocked(tool, decision, detail, next_step=step)
    request = ApprovalRequest(
        tool=tool,
        action=action,
        resource=resource,
        rule_id=decision.rule_id,
        risk=decision.risk,
        reason=decision.reason,
        preview=preview,
        preview_kind=preview_kind,
        effect_class=decision.effect_class or action,
        argv=argv,
        cwd=cwd,
        rule_source=ctx.policy.rule_source(decision.rule_id),
        payload_sha256=payload_sha256,
        preview_complete="\n[truncated " not in preview[-120:],
    )
    ctx.decided_by = None
    try:
        approved = bool(await ctx.approve(request))
    except Exception:  # noqa: BLE001 - approval failure must deny, never allow
        approved = False
    if not approved:
        detail, step = _APPROVAL_NEXT_STEP.get(ctx.decided_by or "human",
                                               _APPROVAL_NEXT_STEP["human"])
        return _blocked(tool, decision, detail, next_step=step)
    return None


_MARKER_MESSAGE = (
    "the text contains the `[redacted]` marker that Panda puts where a secret is; writing "
    "it would replace the real value. Leave secret lines untouched or ask the user"
)


def _schema_problem(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """The first way `args` breaks the tool's JSON Schema, as one actionable sentence."""

    from jsonschema import Draft202012Validator

    strict = dict(schema)
    strict.setdefault("type", "object")
    if "properties" in strict:
        strict.setdefault("additionalProperties", False)
    errors = sorted(Draft202012Validator(strict).iter_errors(args), key=lambda e: list(e.path))
    if not errors:
        return None
    error = errors[0]
    where = ".".join(str(part) for part in error.path) or "arguments"
    if error.validator == "additionalProperties":
        unknown = sorted(set(args) - set(strict.get("properties", {})))
        allowed = ", ".join(strict["properties"])
        return f"unknown argument(s) {', '.join(unknown)}; allowed: {allowed}"
    if error.validator == "required":
        return f"missing required argument: {error.message.split(chr(39))[1]}"
    expected = error.schema.get("type") or error.schema.get("enum") or error.validator
    return f"{where}: {error.message} (expected {expected})"


def _is_git(argv: Sequence[str]) -> bool:
    return bool(argv) and Path(argv[0].replace("\\", "/")).name.lower() in {"git", "git.exe"}


def _fail(tool: str, message: str, *, data: dict[str, Any] | None = None) -> ToolResult:
    payload = {"ok": False, "tool": tool, "error": message, **(data or {})}
    return ToolResult(ok=False, content=_json(payload), data=payload)


def _ok(tool: str, payload: dict[str, Any], *, text: str | None = None) -> ToolResult:
    body = {"ok": True, "tool": tool, **payload}
    return ToolResult(ok=True, content=text if text is not None else _json(body), data=body)

def _bounded(result: ToolResult, limit: int) -> ToolResult:
    """Cut any result above the per-result budget, saying so (R2-408).

    Tools with a natural continuation (read_file) stop early with their own hint;
    this net covers the rest, so no single result can fill the context window.
    """

    size = len(result.content)
    if size <= limit:
        return result
    note = (
        f"\n[result truncated: {size - limit} of {size} characters omitted; "
        "narrow the request (a smaller range, a tighter pattern or a lower limit)]"
    )
    data = {**result.data, "result_truncated": True, "result_chars": size}
    return ToolResult(
        ok=result.ok, content=result.content[:limit] + note, data=data, blocked=result.blocked
    )


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


# Data registries (evidence, numbers) may be larger than an ordinary file, never unbounded.
MAX_DATA_BYTES = 16_000_000


def _bounded_text(path: Path, limit: int | None = None) -> str:
    """Read a corpus file only if it is no larger than `limit` (R2-302)."""

    limit = MAX_DATA_BYTES if limit is None else limit
    size = path.stat().st_size
    if size > limit:
        raise ToolInputError(
            f"{path.name} is {size} bytes, more than {limit}; split it or use grep"
        )
    return path.read_text(encoding="utf-8", errors="replace")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ToolInputError(f"file not found: {path.name}")
    records: list[dict[str, Any]] = []
    for number, line in enumerate(_bounded_text(path).splitlines(), start=1):
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
        payload = json.loads(_bounded_text(path))
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


def _iter_files(
    root: Path, pattern: str, limit: int, *, files_only: bool = False
) -> tuple[list[Path], bool]:
    """Up to `limit` matches in sorted order, and whether more exist (R2-402, R2-413).

    `files_only` counts only regular files, so directories do not use the cap.
    """

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
        if files_only and (candidate.is_symlink() or not candidate.is_file()):
            continue
        if len(found) >= limit:
            return found, True
        found.append(candidate)
    return found, False


GREP_FILE_CAP = 5_000


def _grep_next_step(capped: bool, too_large: int) -> dict[str, str]:
    if capped:
        return {
            "next_step": f"more than {GREP_FILE_CAP} files match the glob and the rest were "
            "not searched; narrow glob (for example corpus/claims/**/*.md)"
        }
    if too_large:
        return {
            "next_step": f"{too_large} file(s) over {MAX_READ_BYTES} bytes were not "
            "searched; a missing match there is not evidence of absence"
        }
    return {}


def _readable_by_policy(ctx: ToolContext, root: Path, candidate: Path) -> bool:
    """True when `read` is plainly allowed for this file, under both its names.

    Search tools authorize the workspace once, so each hit is checked here with
    the same rule `read_file` uses. A symlink is judged by its target as well as
    its own name. `ask` counts as not readable: a search never prompts per file.
    """

    names = {candidate.relative_to(root).as_posix()}
    try:
        names.add(candidate.resolve().relative_to(root.resolve()).as_posix())
    except (OSError, ValueError):
        return False
    return all(ctx.policy.decide("read", name).effect == "allow" for name in names)


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

        problem = _schema_problem(spec.parameters, args)
        if problem:
            # R2-401: the schema the model sees is the one enforced; "false" is not a
            # boolean, an unknown key is not ignored, and a bound is not clipped quietly.
            return _fail(name, f"invalid arguments: {problem}")
        handler = self._handlers[name]
        try:
            return _bounded(await handler(args), self.ctx.max_result_chars())
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
        if start > max(total, 1):
            raise ToolInputError(f"start_line {start} is past the end ({total} lines)")
        last = min(total, start - 1 + limit)
        budget = self.ctx.max_result_chars() - 200  # header and hint
        rendered: list[str] = []
        used = 0
        cut_lines = 0
        end = start - 1
        for number in range(start, last + 1):
            line = lines[number - 1]
            if len(line) > LINE_LIMIT:
                line = f"{line[:LINE_LIMIT]} [line cut: {len(line) - LINE_LIMIT} more chars]"
                cut_lines += 1
            entry = f"{number:>5}  {line}"
            if rendered and used + len(entry) + 1 > budget:
                break
            rendered.append(entry)
            used += len(entry) + 1
            end = number
        header = f"{relative}:{start}-{end} of {total} lines"
        body = f"{header}\n" + ("\n".join(rendered) if rendered else "(empty)")
        truncated = end < total
        payload: dict[str, Any] = {
            "path": relative,
            "total_lines": total,
            "start_line": start,
            "end_line": end,
            "truncated": truncated,
            "rule": decision.rule_id if decision else None,
        }
        if truncated:
            payload["next_start_line"] = end + 1
            reason = "the result budget" if end < last else "max_lines"
            body += (
                f"\n[truncated at line {end} by {reason}; "
                f"call read_file with start_line={end + 1} to continue]"
            )
        if cut_lines:
            payload["lines_cut"] = cut_lines
        return _ok("read_file", payload, text=body)
    async def _list_files(self, args: dict[str, Any]) -> ToolResult:
        pattern = _string_arg(args, "pattern", default="*") or "*"
        limit = _int_arg(args, "limit", default=200, minimum=1, maximum=2_000)
        decision, blocked = await _authorize(
            self.ctx, tool="list_files", action="read", resource="."
        )
        if blocked:
            return blocked
        found, more = _iter_files(self.ctx.workspace.root, pattern, limit)
        entries: list[str] = []
        omitted = 0
        root = self.ctx.workspace.root
        for candidate in found:
            if not _readable_by_policy(self.ctx, root, candidate):
                omitted += 1
                continue
            relative = candidate.relative_to(root.resolve()).as_posix()
            entries.append(relative + ("/" if candidate.is_dir() else ""))
        return _ok(
            "list_files",
            {
                "pattern": pattern,
                "count": len(entries),
                "omitted_by_policy": omitted,
                "truncated": more,
                "entries": entries,
                "rule": decision.rule_id if decision else None,
                **({"next_step": f"more than {limit} entries match; raise limit "
                    "(max 2000) or narrow the pattern"} if more else {}),
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
            re.compile(re.escape(pattern) if fixed else pattern, flags)
        except re.error as exc:
            raise ToolInputError(f"invalid regular expression: {exc}") from exc
        _, blocked = await _authorize(self.ctx, tool="grep", action="read", resource=".")
        if blocked:
            return blocked
        omitted = 0
        root = self.ctx.workspace.root.resolve()
        candidates: list[list[object]] = []
        files, capped = _iter_files(
            self.ctx.workspace.root, glob, GREP_FILE_CAP, files_only=True
        )
        too_large = 0
        for candidate in files:
            if candidate.stat().st_size > MAX_READ_BYTES:
                too_large += 1
                continue
            if not _readable_by_policy(self.ctx, root, candidate):
                omitted += 1
                continue
            info = os.lstat(candidate)
            candidates.append(
                [candidate.relative_to(root).as_posix(), info.st_dev, info.st_ino]
            )
        request = {
            "pattern": pattern,
            "flags": flags,
            "fixed": fixed,
            "limit": limit,
            "max_bytes": MAX_READ_BYTES,
            "files": candidates,
        }
        worker = Path(__file__).with_name("grep_worker.py")
        result = await asyncio.to_thread(
            run_command,
            [sys.executable, "-I", str(worker)],
            cwd=root,
            timeout=GREP_TIMEOUT_SECONDS,
            env=_scrubbed_env(),
            input_text=json.dumps(request),
        )
        if result.timed_out:
            return _fail(
                "grep",
                f"the pattern took longer than {GREP_TIMEOUT_SECONDS:g}s and was stopped; "
                "use fixed_strings or a simpler pattern",
            )
        try:
            found = json.loads(result.stdout)
        except json.JSONDecodeError:
            detail = result.stderr.strip()[-300:] or "no output"
            return _fail("grep", f"the search worker failed: {detail}")
        return _ok(
            "grep",
            {
                "pattern": pattern,
                "glob": glob,
                "files_scanned": found["scanned"],
                "omitted_by_policy": omitted,
                "count": len(found["matches"]),
                "matches": found["matches"],
                "truncated": found["truncated"],
                # R2-402: a count of 0 means "not found" only when the search was complete.
                "search_complete": not (capped or too_large),
                "files_skipped_too_large": too_large,
                "file_cap_reached": capped,
                **_grep_next_step(capped, too_large),
            },
        )

    async def _study_list(self, args: dict[str, Any]) -> ToolResult:
        track = args.get("track")
        status = args.get("status")
        # R2-118: the registry is a file like any other; a read deny on it applies.
        _, blocked = await _authorize(
            self.ctx, tool="study_list", action="read", resource="corpus/studies/INDEX.md"
        )
        if blocked:
            return blocked
        studies = [study.to_dict() for study in load_registry(self.ctx.workspace.root)]
        if isinstance(track, str) and track:
            studies = [study for study in studies if study["track"] == track]
        if isinstance(status, str) and status:
            studies = [study for study in studies if study["status"] == status]
        return _ok("study_list", {"studies": studies, "count": len(studies)})

    async def _study_show(self, args: dict[str, Any]) -> ToolResult:
        study_id = _string_arg(args, "study_id")
        root = self.ctx.workspace.root
        study = find_study(root, study_id)
        if study.path:
            try:
                relative = Path(study.path).relative_to(root).as_posix()
            except ValueError:
                raise ToolInputError("study card is outside the workspace") from None
            _, blocked = await _authorize(
                self.ctx, tool="study_show", action="read", resource=relative
            )
            if blocked:
                return blocked
        card = load_study_card(study, root)
        payload = study.to_dict()
        payload["frontmatter"] = card["frontmatter"]
        payload["body"] = _cap(card["body"], 8_000)
        return _ok("study_show", payload)

    async def _validate(self, args: dict[str, Any]) -> ToolResult:
        release = bool(args.get("release", False))
        workspace = self.ctx.workspace
        _, blocked = await _authorize(
            self.ctx, tool="validate", action="validate", resource=workspace.config.validator
        )
        if blocked:
            return blocked
        validator_hash = validator_sha256(workspace.root, workspace.validator_path)
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
        payload["validator_sha256"] = validator_hash
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
        hash_before: str | None = None
        file_mode = 0o644
        if path.is_file():
            file_mode = stat.S_IMODE(path.stat().st_mode)
            try:
                raw_before = path.read_bytes()
            except OSError as exc:
                raise ToolInputError(f"cannot read existing file: {exc}") from exc
            before = raw_before.decode("utf-8", errors="replace")
            hash_before = hashlib.sha256(raw_before).hexdigest()
        protected = self.ctx.policy.decide("read", relative).effect != "allow"
        if protected:
            # N-01: the read policy denies this file, so its current content must not
            # reach the approval card, the ledger or the model through a diff.
            preview = (
                f"--- {relative} (existing content protected by the read policy; "
                f"sha256 {hash_before or 'none'})\n+++ {relative}\n"
                + "".join(f"+{line}\n" for line in content.splitlines())
            )
            if REDACTED in content:
                return _fail("write_file", _MARKER_MESSAGE)
        else:
            if REDACTED in content and redact_text(before) != before:
                # N-02: the model saw `[redacted]` where a secret is; writing the
                # marker back would destroy the real value.
                return _fail("write_file", _MARKER_MESSAGE)
            preview = _diff_preview(relative, before, content)
        _, blocked = await _authorize(
            self.ctx,
            tool="write_file",
            action="write",
            resource=relative,
            preview=preview,
            preview_kind="diff",
            payload_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        if blocked:
            return blocked
        current = file_sha256(path) if path.is_file() else None
        if current != hash_before:
            return _fail(
                "write_file",
                "the file changed while waiting for approval; read it again and retry",
            )
        changed = before != content
        if changed:
            atomic_write_text(path, content, mode=file_mode)
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
                "sha256_before": hash_before,
                "sha256_after": file_sha256(path) if path.is_file() else None,
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
        if self.ctx.policy.decide("read", relative).effect != "allow":
            # N-01: editing needs the content in a diff; a read-denied file stays unread.
            return _fail(
                "edit_file",
                f"{relative} is protected by the read policy; ask the user to edit it",
            )
        raw = path.read_bytes()
        file_mode = stat.S_IMODE(path.stat().st_mode)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolInputError(
                "file is not valid UTF-8; edit_file refuses to rewrite it because "
                "bytes would be lost (use write_file with the full content)"
            ) from None
        ending = _line_ending(text)
        before = text.replace(ending, "\n") if ending != "\n" else text
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        count = before.count(old)
        if count == 0:
            raise ToolInputError("old_string was not found; read the file before editing")
        if count > 1 and not replace_all:
            raise ToolInputError(
                f"old_string appears {count} times; provide more context or set replace_all"
            )
        if REDACTED in new and redact_text(before) != before:
            return _fail("edit_file", _MARKER_MESSAGE)
        after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
        preview = _diff_preview(relative, before, after)
        _, blocked = await _authorize(
            self.ctx,
            tool="edit_file",
            action="edit",
            resource=relative,
            preview=preview,
            preview_kind="diff",
            payload_sha256=hashlib.sha256(after.encode("utf-8")).hexdigest(),
        )
        if blocked:
            return blocked
        data = (after.replace("\n", ending) if ending != "\n" else after).encode("utf-8")
        if not path.is_file() or path.read_bytes() != raw:
            return _fail(
                "edit_file",
                "the file changed while waiting for approval; read it again and retry",
            )
        atomic_write_bytes(path, data, mode=file_mode)
        try:
            written = _read_exact(path)  # no newline translation, so CRLF is compared as is
        except OSError as exc:
            return _fail("edit_file", f"post-write verification failed: {exc}")
        if written != data.decode("utf-8"):
            return _fail(
                "edit_file",
                "post-write verification failed: content differs after edit",
            )
        return _ok(
            "edit_file",
            {
                "path": relative,
                "replacements": count if replace_all else 1,
                "bytes": len(data),
                "sha256_before": hashlib.sha256(raw).hexdigest(),
                "sha256_after": hashlib.sha256(data).hexdigest(),
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
        # The preview keeps token boundaries (R2-117): `['a b']` and `['a', 'b']`
        # must not look the same to the person approving.
        shown = shlex.join(argv)
        payload_hash = hashlib.sha256(
            json.dumps({"argv": argv, "cwd": cwd_relative}).encode("utf-8")
        ).hexdigest()
        decision, blocked = await _authorize(
            self.ctx,
            tool="run_shell",
            action="shell",
            resource=command_text,
            preview=shown,
            preview_kind="command",
            argv=argv,
            cwd=cwd_relative,
            payload_sha256=payload_hash,
        )
        if blocked:
            return blocked
        effective, env = list(argv), _scrubbed_env()
        if decision is not None and decision.effect == "allow" and _is_git(argv):
            # Allowed git runs with nobody watching: the repository must not choose
            # a program, and content commands must not print denied files.
            hardened = gitsafe.harden(
                argv,
                root=self.ctx.workspace.root,
                cwd=cwd,
                base_env=env,
                denied_reads=self.ctx.policy.denied_read_patterns(),
            )
            if hardened.hazard is not None:
                escalated = replace(
                    decision,
                    effect="ask",
                    risk="high",
                    rule_id="shell-git-hazard",
                    reason=f"git cannot be isolated here: {hardened.hazard}",
                    effect_class="exec_unknown",
                )
                blocked = await _ask(
                    self.ctx,
                    escalated,
                    tool="run_shell",
                    action="shell",
                    resource=command_text,
                    preview=shown,
                    preview_kind="command",
                    argv=tuple(argv),
                    cwd=cwd_relative,
                    payload_sha256=payload_hash,
                )
                if blocked:
                    return blocked
            else:
                effective, env = list(hardened.argv), dict(hardened.env)
        cancel = threading.Event()
        try:
            result = await asyncio.to_thread(
                run_command, effective, cwd=cwd, timeout=timeout, env=env, cancel=cancel
            )
        except asyncio.CancelledError:
            cancel.set()  # the thread kills the process group; do not wait for it
            raise
        payload = {
            "argv": argv,
            **({"effective_argv": effective} if effective != list(argv) else {}),
            "cwd": cwd_relative,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "output_truncated": result.output_truncated,
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
                # R2-413: `total` counts the matches, like claim_list; the file total is
                # separate so a filter never reads as "more to page through".
                "total": len(summaries),
                "records_in_file": len(records),
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

    def _claim_records(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Readable claim cards, and the cards left out keyed by file stem (R2-403).

        The reason is `policy`, `link` or `too_large`; the caller reports them so a
        missing claim is never mistaken for an absent one.
        """

        directory = self.ctx.workspace.root / "corpus/claims"
        if not directory.is_dir():
            raise ToolInputError("claims directory not found: corpus/claims")
        records: list[dict[str, Any]] = []
        omitted: dict[str, str] = {}
        root = self.ctx.workspace.root
        for path in sorted(directory.glob("*.md")):
            try:
                ensure_plain_path(root, path)
            except ConfigError:
                omitted[path.stem] = "link"  # SEC-011
                continue
            if not _readable_by_policy(self.ctx, root, path):
                omitted[path.stem] = "policy"
                continue
            if path.stat().st_size > MAX_READ_BYTES:
                omitted[path.stem] = "too_large"  # R2-302
                continue
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
        return records, omitted

    async def _claim_list(self, args: dict[str, Any]) -> ToolResult:
        _, blocked = await _authorize(
            self.ctx, tool="claim_list", action="read", resource="corpus/claims"
        )
        if blocked:
            return blocked
        records, omitted = self._claim_records()
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
                "omitted_by_policy": sum(1 for why in omitted.values() if why == "policy"),
                "omitted_links": sum(1 for why in omitted.values() if why == "link"),
                "omitted_too_large": sum(1 for why in omitted.values() if why == "too_large"),
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
        records, omitted = self._claim_records()
        for record in records:
            if _record_id(record, ("claim_id", "id")) == claim_id:
                payload = dict(record)
                payload["body"] = _cap(str(payload.get("body", "")), 8_000)
                return _ok("claim_show", {"claim": payload})
        why = omitted.get(claim_id)
        if why == "policy":
            decision = self.ctx.policy.decide("read", f"corpus/claims/{claim_id}.md")
            return _blocked(
                "claim_show",
                decision,
                "policy denies reading this claim card",
                next_step=_denied_next_step(
                    decision, self.ctx.policy.rule_source(decision.rule_id)
                ),
            )
        if why == "link":
            raise ToolInputError(f"claim card {claim_id}.md is a link and is not read")
        if why == "too_large":
            raise ToolInputError(f"claim card {claim_id}.md is larger than {MAX_READ_BYTES} bytes")
        if omitted:
            raise ToolInputError(
                f"claim not found: {claim_id} ({len(omitted)} card(s) were not readable; "
                "their ids are taken from the file name only)"
            )
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