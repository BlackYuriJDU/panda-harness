"""The task's MCP applications, discovered once and frozen for the session.

Discovery runs before the first model call: changing the tool list later would
invalidate signed Claude thinking. A small catalog becomes direct function
tools (`mcp__<server>__<tool>`); a large one (task catalogs can reach ~500 KB)
is reached through two tools, `mcp_find` and `mcp_call`, so the request stays
small. Actions taken in these apps are deliverables: graders read their state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from panda_core.tools import ToolResult

import taskbridge

DIRECT_MAX_TOOLS = 128
DIRECT_MAX_BYTES = 40_000
FIND_LIMIT = 15
RESULT_CHARS = 30_000
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass
class McpTool:
    server: str
    name: str
    description: str
    schema: dict[str, Any]
    function_name: str = ""


@dataclass
class McpTools:
    """Catalog and calls for every MCP server of one task."""

    clients: dict[str, Any] = field(default_factory=dict)
    tools: list[McpTool] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    direct: bool = True

    @classmethod
    async def discover(cls, session: taskbridge.Session) -> McpTools:
        catalog = cls()
        for server in session.mcp_servers:
            name, url = str(server.get("name") or "apps"), str(server.get("url") or "")
            if not url:
                continue
            client = taskbridge.MCPClient(session, url, client="panda-ple")
            try:
                await client.initialize()
                listed = await client.list_tools()
            except (taskbridge.MCPError, taskbridge.TaskError) as exc:
                catalog.errors.append(f"{name}: {exc}")
                continue
            catalog.clients[name] = client
            for item in listed:
                catalog.tools.append(
                    McpTool(
                        server=name,
                        name=item["name"],
                        description=item.get("description", ""),
                        schema=item.get("inputSchema") or {"type": "object", "properties": {}},
                    )
                )
        size = len(json.dumps([t.__dict__ for t in catalog.tools], default=str))
        catalog.direct = len(catalog.tools) <= DIRECT_MAX_TOOLS and size <= DIRECT_MAX_BYTES
        catalog._assign_names()
        return catalog

    def _assign_names(self) -> None:
        used: set[str] = set()
        for tool in self.tools:
            base = _NAME_RE.sub("_", f"mcp__{tool.server}__{tool.name}")[:60]
            name, n = base, 2
            while name in used:
                name, n = f"{base[:57]}_{n}", n + 1
            used.add(name)
            tool.function_name = name

    def summary(self) -> str:
        """One line per server for the system prompt."""

        if not self.clients and not self.errors:
            return "No MCP applications in this task."
        lines = []
        for server in self.clients:
            count = sum(1 for tool in self.tools if tool.server == server)
            lines.append(f"- MCP server `{server}`: {count} tools")
        lines += [f"- MCP server unavailable: {error}" for error in self.errors]
        mode = (
            "Their tools are listed as functions named mcp__<server>__<tool>."
            if self.direct
            else "Search them with mcp_find, then call them with mcp_call."
        )
        return "\n".join([*lines, mode])

    # ---- tool surface ----
    def specs(self) -> list[dict[str, Any]]:
        if not self.tools:
            return []
        if self.direct:
            return [
                {
                    "type": "function",
                    "function": {
                        "name": tool.function_name,
                        "description": f"[{tool.server}] {tool.description}"[:1024],
                        "parameters": tool.schema,
                    },
                }
                for tool in self.tools
            ]
        return [
            {
                "type": "function",
                "function": {
                    "name": "mcp_find",
                    "description": "Search the task's MCP application tools by keywords; "
                    "returns names, descriptions, and argument schemas.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "mcp_call",
                    "description": "Call one MCP application tool found with mcp_find.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "server": {"type": "string"},
                            "tool": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                        "required": ["server", "tool"],
                    },
                },
            },
        ]

    def handles(self, name: str) -> bool:
        if not self.tools:
            return False
        if self.direct:
            return any(tool.function_name == name for tool in self.tools)
        return name in {"mcp_find", "mcp_call"}

    async def call(self, name: str, args: dict[str, Any], *, vision: bool = False) -> ToolResult:
        if name == "mcp_find":
            return self._find(str(args.get("query") or ""))
        if name == "mcp_call":
            tool = next(
                (
                    t
                    for t in self.tools
                    if t.server == args.get("server") and t.name == args.get("tool")
                ),
                None,
            )
            arguments = args.get("arguments") or {}
        else:
            tool = next((t for t in self.tools if t.function_name == name), None)
            arguments = args
        if tool is None:
            return _error(
                "unknown MCP tool", "not in the task's catalog", "use mcp_find to get exact names"
            )
        try:
            result = await self.clients[tool.server].call_tool(tool.name, arguments)
        except taskbridge.MCPError as exc:
            return _error(f"{tool.name} failed", str(exc), "check the arguments and try again")
        return _result(result, vision=vision)

    def _find(self, query: str) -> ToolResult:
        words = [w for w in re.split(r"\W+", query.lower()) if w]
        scored = []
        for tool in self.tools:
            text = f"{tool.name} {tool.description}".lower()
            score = sum(text.count(word) for word in words)
            if score:
                scored.append((score, tool))
        scored.sort(key=lambda item: -item[0])
        matches = [
            {
                "server": tool.server,
                "tool": tool.name,
                "description": tool.description[:400],
                "arguments": tool.schema,
            }
            for _, tool in scored[:FIND_LIMIT]
        ]
        body = {"matches": matches, "total_tools": len(self.tools)}
        return ToolResult(ok=True, content=_cap(json.dumps(body, ensure_ascii=False)))


def _cap(text: str) -> str:
    if len(text) <= RESULT_CHARS:
        return text
    return text[:RESULT_CHARS] + f"\n[truncated {len(text) - RESULT_CHARS} chars]"


def _error(error: str, cause: str, next_step: str) -> ToolResult:
    body = {"ok": False, "error": error, "cause": cause, "next_step": next_step}
    return ToolResult(ok=False, content=json.dumps(body, ensure_ascii=False), data=body)


def _result(result: dict[str, Any], *, vision: bool) -> ToolResult:
    texts: list[str] = []
    images: list[tuple[str, str]] = []
    for part in result.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
        elif part.get("type") == "image" and vision and part.get("data"):
            images.append((str(part.get("mimeType") or "image/png"), str(part["data"])))
        else:
            texts.append(json.dumps(part, ensure_ascii=False)[:2000])
    if result.get("structuredContent") is not None and not texts:
        texts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    ok = not result.get("isError")
    tool_result = ToolResult(ok=ok, content=_cap("\n".join(texts) or "(empty result)"))
    tool_result.images.extend(images)
    return tool_result


__all__ = ["McpTool", "McpTools"]
