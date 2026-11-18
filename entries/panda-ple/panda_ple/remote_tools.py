"""Tools that act inside the PLE task container through the serve protocol.

Every tool runs through the evaluator's `exec`, `read_file`, `write_file`, and
`http` requests; nothing touches the serve container's own filesystem. Tool
errors follow one contract, `{"ok": false, "error", "cause", "next_step"}`, so
the model always learns what failed and what to try next.

Original project files are never lost: before the first overwrite of an
existing file outside `/tmp`, a copy goes to `/tmp/panda-originals/`. Shell
commands are free inside the container (it is the sandbox); only the evaluator
limits them.
"""

from __future__ import annotations

import base64
import json
import shlex
from collections.abc import Awaitable, Callable
from typing import Any

from panda_core.tools import IMAGE_SIGNATURES, MAX_IMAGE_BYTES, ToolResult

import taskbridge
from panda_ple.mcp_tools import McpTools

# A path in the task container, not on this machine.
BACKUP_ROOT = "/tmp/panda-originals"  # noqa: S108
OUTPUT_CHARS = 30_000  # what one tool result may put into the context
READ_LINES = 400
# Seconds kept free at the end of the task for wrapping up.
EXEC_RESERVE_SEC = 30

Handler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


def failure(error: str, cause: str, next_step: str) -> ToolResult:
    body = {"ok": False, "error": error, "cause": cause, "next_step": next_step}
    return ToolResult(ok=False, content=json.dumps(body, ensure_ascii=False), data=body)


def head_tail(text: str, limit: int = OUTPUT_CHARS) -> str:
    """Keep the start and the end of a long output: errors usually sit at the end."""

    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - limit} chars omitted ...]\n{text[-half:]}"


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


class RemoteToolset:
    """The agent's `Toolset` for one task: shell, files, images, and MCP apps."""

    # Scratch space in the task container: never backed up.
    scratch_prefix = "/tmp/"  # noqa: S108

    def __init__(
        self,
        session: taskbridge.Session,
        *,
        vision: bool = False,
        mcp: McpTools | None = None,
        backup_root: str = BACKUP_ROOT,
    ) -> None:
        self.session = session
        self.backup_root = backup_root.rstrip("/")
        self.vision = vision
        self.mcp = mcp
        self.written: list[str] = []  # deliverable candidates, in first-write order
        self._backed_up: set[str] = set()
        self._handlers: dict[str, Handler] = {
            "bash": self._bash,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
        }
        if vision:
            self._handlers["view_image"] = self._view_image

    # ---- Toolset ----
    def to_openai(self) -> list[dict[str, Any]]:
        specs = [
            _function(
                "bash",
                "Run a bash command in the task container (fresh subshell in the working "
                "directory; `cd` does not persist). Use it to list files, run Python "
                "scripts, convert documents, and check results.",
                {
                    "command": {"type": "string"},
                    "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                ["command"],
            ),
            _function(
                "read_file",
                "Read a text file with line numbers. For binary formats (PDF, XLSX, DOCX, "
                "DXF, IFC) use bash with a Python library instead.",
                {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000},
                },
                ["path"],
            ),
            _function(
                "write_file",
                "Create or overwrite a text file (parent directories are created). Set "
                "append to add to the end.",
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "append": {"type": "boolean"},
                },
                ["path", "content"],
            ),
            _function(
                "edit_file",
                "Replace an exact text span in a file. old_text must occur exactly once "
                "unless replace_all is true.",
                {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                ["path", "old_text", "new_text"],
            ),
        ]
        if self.vision:
            specs.append(
                _function(
                    "view_image",
                    "Look at a PNG, JPEG, GIF, or WebP image (drawings, plots, PDF pages "
                    "rendered to PNG with bash first).",
                    {"path": {"type": "string"}},
                    ["path"],
                )
            )
        if self.mcp is not None:
            specs.extend(self.mcp.specs())
        return specs

    async def call(self, name: str, arguments: dict[str, Any] | str | None) -> ToolResult:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                return failure(
                    "arguments are not valid JSON", exc.msg, "resend the call with a JSON object"
                )
        args = arguments if isinstance(arguments, dict) else {}
        handler = self._handlers.get(name)
        try:
            if handler is not None:
                return await handler(args)
            if self.mcp is not None and self.mcp.handles(name):
                return await self.mcp.call(name, args, vision=self.vision)
        except taskbridge.TaskError as exc:
            return failure(
                f"{name} failed in the task container",
                str(exc),
                "check the path or command and try again",
            )
        except (KeyError, TypeError, ValueError) as exc:
            return failure(
                f"invalid arguments for {name}", str(exc), "follow the tool's parameter schema"
            )
        return failure(
            f"unknown tool: {name}", "not in this session's tool list", "use a listed tool"
        )

    # ---- helpers ----
    def _timeout(self, requested: Any) -> int:
        limit = int(self.session.limits.get("exec_timeout_sec") or 600)
        wanted = int(requested) if isinstance(requested, (int, float)) else 120
        left = self.session.time_left()
        if left is not None:
            limit = min(limit, max(1, int(left) - EXEC_RESERVE_SEC))
        return max(1, min(wanted, limit))

    def _note_written(self, path: str) -> None:
        if path not in self.written:
            self.written.append(path)

    async def _backup(self, path: str) -> None:
        """Copy an existing file aside before its first overwrite."""

        if path in self._backed_up or path.startswith(self.scratch_prefix):
            return
        self._backed_up.add(path)
        target = f"{self.backup_root}/{path.lstrip('/')}"
        quoted, backup = shlex.quote(path), shlex.quote(target)
        await self.session.exec(
            f"if [ -f {quoted} ] && [ ! -e {backup} ]; then "
            f"mkdir -p \"$(dirname {backup})\" && cp -p {quoted} {backup}; fi",
            timeout_sec=60,
        )

    def _absolute(self, path: str) -> str:
        if path.startswith("/"):
            return path
        return f"{self.session.workdir.rstrip('/')}/{path}"

    # ---- handlers ----
    async def _bash(self, args: dict[str, Any]) -> ToolResult:
        command = str(args["command"])
        timeout = self._timeout(args.get("timeout_sec"))
        result = await self.session.exec(command, timeout_sec=timeout)
        code = result.get("exit_code")
        header = f"exit_code: {code}"
        if result.get("timed_out"):
            header += " (timed out: split the work or run it in the background)"
        if result.get("truncated"):
            header += " (output truncated by the evaluator)"
        output = head_tail(str(result.get("output") or ""))
        return ToolResult(ok=code == 0, content=f"{header}\n{output}", data={"exit_code": code})

    async def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path = str(args["path"])
        start = max(1, int(args.get("start_line") or 1))
        count = max(1, min(2000, int(args.get("max_lines") or READ_LINES)))
        raw = await self.session.read_file(path)
        if b"\x00" in raw[:8192]:
            return failure(
                f"{path} is a binary file ({len(raw)} bytes)",
                "read_file shows text only",
                "extract it with a Python library through bash (pdfplumber, openpyxl, "
                "python-docx, ezdxf, ifcopenshell), or view_image for images",
            )
        lines = raw.decode("utf-8", "replace").splitlines()
        end = min(len(lines), start - 1 + count)
        numbered = "\n".join(f"{n:>6}  {lines[n - 1]}" for n in range(start, end + 1))
        header = f"{path}:{start}-{end} of {len(lines)} lines"
        return ToolResult(ok=True, content=head_tail(f"{header}\n{numbered}"))

    async def _write_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._absolute(str(args["path"]))
        content = args["content"]
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        append = bool(args.get("append"))
        if not append:
            await self._backup(path)
        size = await self.session.write_file(path, content, append=append)
        self._note_written(path)
        return ToolResult(ok=True, content=f"wrote {path} ({size} bytes)", data={"size": size})

    async def _edit_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._absolute(str(args["path"]))
        old, new = str(args["old_text"]), str(args["new_text"])
        if not old:
            return failure("old_text is empty", "nothing to find", "pass the exact text to replace")
        text = (await self.session.read_file(path)).decode("utf-8")
        found = text.count(old)
        if found == 0:
            return failure(
                "old_text not found",
                f"{path} has no exact match (whitespace counts)",
                "read_file the region and copy the text exactly",
            )
        if found > 1 and not args.get("replace_all"):
            return failure(
                f"old_text occurs {found} times",
                "the replacement would be ambiguous",
                "add surrounding lines to make it unique, or set replace_all",
            )
        await self._backup(path)
        await self.session.write_file(path, text.replace(old, new))
        self._note_written(path)
        return ToolResult(ok=True, content=f"edited {path} ({found} replacement(s))")

    async def _view_image(self, args: dict[str, Any]) -> ToolResult:
        path = str(args["path"])
        raw = await self.session.read_file(path)
        if len(raw) > MAX_IMAGE_BYTES:
            return failure(
                f"{path} is {len(raw)} bytes",
                f"images above {MAX_IMAGE_BYTES} bytes are not sent",
                "downscale or crop it with Python (PIL) first",
            )
        media_type = next(
            (kind for magic, kind in IMAGE_SIGNATURES if raw.startswith(magic)),
            "image/webp" if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" else None,
        )
        if media_type is None:
            return failure(
                f"{path} is not a PNG, JPEG, GIF, or WebP image",
                "unknown image signature",
                "render it to PNG first (for PDFs: pdftoppm -png -r 100)",
            )
        result = ToolResult(ok=True, content=json.dumps({"path": path, "bytes": len(raw)}))
        result.images.append((media_type, base64.b64encode(raw).decode("ascii")))
        return result


__all__ = ["RemoteToolset", "failure", "head_tail"]
