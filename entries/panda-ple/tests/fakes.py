"""Test doubles: an in-process task container, an MCP app, and a WebSocket evaluator.

`LocalContainer` executes protocol requests against a local directory, the way
the real evaluator executes them in the task container. `McpApp` answers MCP
JSON-RPC like the kit's `kit check` stub (one `secret_word` tool, SSE replies).
`evaluate()` drives a harness server through the real serve protocol.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from pathlib import Path
from typing import Any

import websockets

MCP_URL = "http://127.0.0.1:8000/mcp/"


class McpApp:
    def __init__(self, tools: int = 1) -> None:
        self.word = "MCP-" + secrets.token_hex(4).upper()
        self.calls: list[str] = []
        self.tools = [
            {
                "name": "secret_word",
                "description": "Returns the secret word.",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ] + [
            {
                "name": f"filler_{n}",
                "description": f"Unrelated tool number {n}.",
                "inputSchema": {"type": "object", "properties": {}},
            }
            for n in range(tools - 1)
        ]

    def handle(self, body: bytes) -> dict[str, Any]:
        message = json.loads(body or b"{}")
        if "id" not in message:
            return {"status": 202, "headers": {}, "body": b""}
        method = message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            result: dict[str, Any] | None = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "apps", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            self.calls.append(params.get("name"))
            text = self.word if params.get("name") == "secret_word" else "nothing"
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            result = None
        answer = {"jsonrpc": "2.0", "id": message["id"]}
        answer.update(
            {"result": result}
            if result is not None
            else {"error": {"code": -32601, "message": "unknown"}}
        )
        return {
            "status": 200,
            "headers": {"Content-Type": "text/event-stream", "Mcp-Session-Id": "test"},
            "body": b"event: message\ndata: " + json.dumps(answer).encode() + b"\n\n",
        }


class LocalContainer:
    """Executes serve-protocol requests in `root` (the task's working directory)."""

    def __init__(self, root: Path, mcp: McpApp | None = None) -> None:
        self.root = root
        self.mcp = mcp
        self.requests: list[dict[str, Any]] = []

    def _path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    async def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        kind = request["type"]
        if kind == "exec":
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                request["command"],
                cwd=self.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                limit = request.get("timeout_sec") or 120
                out, _ = await asyncio.wait_for(proc.communicate(), limit)
            except TimeoutError:
                proc.kill()
                return {"exit_code": 124, "output": "", "truncated": False, "timed_out": True}
            return {
                "exit_code": proc.returncode,
                "output": out.decode("utf-8", "replace"),
                "truncated": False,
                "timed_out": False,
            }
        if kind == "read_file":
            path = self._path(request["path"])
            if not path.is_file():
                return {"ok": False, "error": f"no such file: {request['path']}"}
            data = path.read_bytes()
            return {
                "ok": True,
                "content_b64": base64.b64encode(data).decode(),
                "size": len(data),
                "truncated": False,
            }
        if kind == "write_file":
            path = self._path(request["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            data = base64.b64decode(request["content_b64"])
            with path.open("ab" if request.get("append") else "wb") as handle:
                handle.write(data)
            return {"ok": True, "size": path.stat().st_size}
        if kind == "http":
            if self.mcp is None or not request["url"].startswith(MCP_URL):
                return {"ok": False, "error": "not an MCP server of this task"}
            reply = self.mcp.handle(base64.b64decode(request.get("body_b64") or ""))
            return {
                "ok": True,
                "status": reply["status"],
                "headers": reply["headers"],
                "body_b64": base64.b64encode(reply["body"]).decode(),
                "truncated": False,
            }
        return {"ok": False, "error": f"unknown request {kind}"}


class FakeSession:
    """A `taskbridge.Session` stand-in for unit tests, backed by `LocalContainer`."""

    def __init__(self, root: Path, mcp: McpApp | None = None, time_left: float | None = None):
        self.container = LocalContainer(root, mcp)
        self.workdir = str(root)
        self.instruction = "test task"
        self.limits = {"exec_timeout_sec": 600}
        self.mcp_servers = (
            [{"name": "apps", "url": MCP_URL, "transport": "streamable-http"}] if mcp else []
        )
        self._time_left = time_left
        self.final_message = ""
        self.logs: list[str] = []
        self.usages: list[dict[str, Any]] = []

    def time_left(self) -> float | None:
        return self._time_left

    async def exec(self, command: str, timeout_sec: int = 120) -> dict[str, Any]:
        return await self.container.answer(
            {"type": "exec", "command": command, "timeout_sec": timeout_sec}
        )

    async def read_file(self, path: str) -> bytes:
        import taskbridge

        result = await self.container.answer({"type": "read_file", "path": path})
        if not result.get("ok"):
            raise taskbridge.TaskError(result.get("error"))
        return base64.b64decode(result["content_b64"])

    async def write_file(self, path: str, data: bytes | str, append: bool = False) -> int:
        payload = data.encode() if isinstance(data, str) else data
        result = await self.container.answer(
            {
                "type": "write_file",
                "path": path,
                "append": append,
                "content_b64": base64.b64encode(payload).decode(),
            }
        )
        return int(result["size"])

    async def http(self, url, method="POST", headers=None, body=b"", timeout_sec=120):
        import taskbridge

        result = await self.container.answer(
            {"type": "http", "url": url, "body_b64": base64.b64encode(body).decode()}
        )
        if not result.get("ok", True):
            raise taskbridge.TaskError(result.get("error"))
        return {
            "status": result["status"],
            "headers": result["headers"],
            "body": base64.b64decode(result["body_b64"]),
            "truncated": False,
        }

    async def log(self, text: str) -> None:
        self.logs.append(text)

    async def usage(self, model, input_tokens=0, output_tokens=0, cache_read_tokens=0):
        self.usages.append(
            {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
            }
        )


async def evaluate(
    url: str,
    container: LocalContainer,
    instruction: str,
    *,
    mcp: bool = False,
    agent_timeout_sec: int = 600,
    cancel_after: float | None = None,
) -> dict[str, Any]:
    """Run one task against a harness server, as the evaluator does."""

    logs: list[str] = []
    usages: list[dict[str, Any]] = []
    async with websockets.connect(url, max_size=16 << 20) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "task",
                    "protocol": 1,
                    "session": "test",
                    "instruction": instruction,
                    "workdir": str(container.root),
                    "limits": {
                        "agent_timeout_sec": agent_timeout_sec,
                        "exec_timeout_sec": 600,
                        "max_output_bytes": 100_000,
                        "max_write_bytes": 10_000_000,
                    },
                    "mcp_servers": (
                        [{"name": "apps", "url": MCP_URL, "transport": "streamable-http"}]
                        if mcp
                        else []
                    ),
                }
            )
        )
        loop = asyncio.get_running_loop()
        cancel_at = None if cancel_after is None else loop.time() + cancel_after
        cancelled_at: float | None = None
        pending: set[asyncio.Task[None]] = set()

        async def reply(request: dict[str, Any]) -> None:
            result = await container.answer(request)
            await ws.send(json.dumps({"type": "result", "id": request["id"], **result}))

        while True:
            timeout = None if cancel_at is None else max(0.0, cancel_at - loop.time())
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout)
            except TimeoutError:
                await ws.send(json.dumps({"type": "cancel", "reason": "timeout"}))
                cancelled_at, cancel_at = loop.time(), None
                continue
            message = json.loads(frame)
            kind = message.get("type")
            if kind == "log":
                logs.append(message["text"])
            elif kind == "usage":
                usages.append(message)
            elif kind in {"done", "error"}:
                for task in pending:
                    task.cancel()
                after_cancel = None if cancelled_at is None else loop.time() - cancelled_at
                return {
                    "end": message,
                    "logs": logs,
                    "usage": usages,
                    "seconds_after_cancel": after_cancel,
                }
            elif cancelled_at is not None:
                await ws.send(
                    json.dumps(
                        {
                            "type": "result",
                            "id": message.get("id"),
                            "ok": False,
                            "error": "cancelled: the time is up",
                        }
                    )
                )
            else:
                task = asyncio.create_task(reply(message))
                pending.add(task)
                task.add_done_callback(pending.discard)
