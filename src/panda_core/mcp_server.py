"""MCP stdio server exposing the Panda service.

Direct tool names are unprefixed (`validate`, `study_list`, ...). OpenCode names
them `<server>_<tool>`; with the server named `panda` they become
`panda_validate`, `panda_study_list`, and so on.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any, cast

from panda_core.errors import ConfigError, DependencyError, PandaError
from panda_core.service import PandaService
from panda_core.session import SessionLog
from panda_core.workspace import Workspace

INSTRUCTIONS = """Panda is a fail-closed evidence harness.

Use `workspace_info` before acting. Use `study_list` and `study_show` to route
work through a registered study. Use `validate` after changing corpus content;
a failed check is a stop condition, not a suggestion. Use `policy_check` before
an action whose write scope or license is uncertain. Use `session_note` to leave
a durable observation in the provenance log.

Every claim must carry a study_id and track. Every number must resolve to an
evidence_id. Every evidence record must resolve to a source_path:source_line
with a hash and a license. Restricted or non-commercial sources are
metadata-only. Never push, publish, or rewrite history. If a rule cannot be
satisfied, return a blocked result with the exact missing evidence.
"""

TRANSPORTS = ("stdio", "streamable-http")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_transport(transport: str, host: str, port: int) -> None:
    """Keep the HTTP surface loopback-only; it has no authentication layer."""

    if transport not in TRANSPORTS:
        raise ConfigError(
            f"unsupported MCP transport: {transport}",
            hint=f"use one of: {', '.join(TRANSPORTS)}",
        )
    if transport == "stdio":
        return
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"MCP HTTP transport is loopback-only, refusing host: {host}",
            hint="bind 127.0.0.1 or localhost; use an authenticated proxy for remote access",
        )
    if not 1 <= port <= 65535:
        raise ConfigError(f"MCP HTTP port is out of range: {port}")


def create_server(
    service: PandaService,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise DependencyError(
            "the MCP server dependency is not installed",
            hint="install it with `pip install 'panda-harness[mcp]'`",
        ) from exc

    server = FastMCP("panda", instructions=INSTRUCTIONS, host=host, port=port)
    # R2-228: FastMCP calls a sync tool on its event loop, so a `validate` (corpus
    # code, up to minutes) froze every client. Tools are async and run the service
    # in a worker thread; one lock keeps ledger writes in order.
    lock = threading.Lock()

    async def call(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        def locked() -> Any:
            with lock:
                return fn(*args, **kwargs)

        return await asyncio.to_thread(locked)

    @server.tool()
    async def workspace_info() -> dict[str, Any]:
        """Return the workspace root, model, validator, policy, and session state."""

        return cast(dict[str, Any], await call(service.workspace_info))

    @server.tool()
    async def validate(release: bool = False) -> dict[str, Any]:
        """Run the canonical corpus validator and return all check results.

        Args:
            release: Also enforce the public-face release checks.
        """

        return cast(dict[str, Any], await call(service.validate, release=release))

    @server.tool()
    async def policy_check(
        action: str, resource: str, approved: bool = False
    ) -> dict[str, Any]:
        """Evaluate the policy for one action/resource pair.

        Args:
            action: read, write, edit, shell or validate (enforced); other actions are
                answered with enforced: false.
            resource: A workspace-relative path, shell command, or tool name.
            approved: Deprecated and ignored. An MCP client cannot approve an action.
        """

        return cast(
            dict[str, Any],
            await call(service.policy_check, action, resource, approved=approved),
        )

    @server.tool()
    async def study_list(
        track: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List registered studies, optionally filtered by track or status."""

        return cast(
            list[dict[str, Any]], await call(service.study_list, track=track, status=status)
        )

    @server.tool()
    async def study_show(study_id: str) -> dict[str, Any]:
        """Return one study card, its frontmatter, and its body."""

        return cast(dict[str, Any], await call(service.study_show, study_id))

    @server.tool()
    async def session_note(message: str) -> dict[str, Any]:
        """Append a durable note to the current provenance session."""

        return cast(dict[str, Any], await call(service.note, message))

    @server.resource("panda://workspace")
    async def workspace_resource() -> str:
        """Workspace snapshot as JSON."""

        return json.dumps(await call(service.workspace_info), ensure_ascii=False, indent=2)

    @server.resource("panda://studies")
    async def studies_resource() -> str:
        """Study registry as JSON."""

        return json.dumps(await call(service.study_list), ensure_ascii=False, indent=2)

    return server


def serve(
    root: Path,
    *,
    session_file: Path | None = None,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    validate_transport(transport, host, port)
    workspace = Workspace.from_root(root)
    session = None
    if session_file and session_file.is_file():
        session = SessionLog.open(
            session_file, redact_enabled=workspace.config.logging.redact
        )
    service = PandaService(
        workspace,
        session=session,
        autocreate_session=session is None,
        session_label="mcp",
    )
    server = create_server(service, host=host, port=port)
    if transport != "stdio":
        print(
            f"panda mcp: http://{host}:{port}/mcp (loopback only, no auth)",
            file=sys.stderr,
            flush=True,
        )
    try:
        server.run(transport=transport)
    finally:
        service.close()


__all__ = [
    "INSTRUCTIONS",
    "LOOPBACK_HOSTS",
    "TRANSPORTS",
    "create_server",
    "serve",
    "validate_transport",
    "PandaError",
]
