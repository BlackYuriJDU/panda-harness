"""MCP stdio server exposing the Panda service.

Direct tool names are unprefixed (`validate`, `study_list`, ...). OpenCode names
them `<server>_<tool>`; with the server named `panda` they become
`panda_validate`, `panda_study_list`, and so on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from panda_core.errors import DependencyError, PandaError
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


def create_server(service: PandaService) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise DependencyError(
            "the MCP server dependency is not installed",
            hint="install it with `pip install 'panda-harness[mcp]'`",
        ) from exc

    server = FastMCP("panda", instructions=INSTRUCTIONS)

    @server.tool()
    def workspace_info() -> dict[str, Any]:
        """Return the workspace root, model, validator, policy, and session state."""

        return service.workspace_info()

    @server.tool()
    def validate(release: bool = False) -> dict[str, Any]:
        """Run the canonical corpus validator and return all check results.

        Args:
            release: Also enforce the public-face release checks.
        """

        return service.validate(release=release)

    @server.tool()
    def policy_check(action: str, resource: str, approved: bool = False) -> dict[str, Any]:
        """Evaluate the policy for one action/resource pair.

        Args:
            action: read, write, edit, delete, shell, network, git, release, spend, or tool.
            resource: A workspace-relative path, shell command, or tool name.
            approved: Treat an `ask` decision as approved for this evaluation only.
        """

        return service.policy_check(action, resource, approved=approved)

    @server.tool()
    def study_list(
        track: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List registered studies, optionally filtered by track or status."""

        return service.study_list(track=track, status=status)

    @server.tool()
    def study_show(study_id: str) -> dict[str, Any]:
        """Return one study card, its frontmatter, and its body."""

        return service.study_show(study_id)

    @server.tool()
    def session_note(message: str) -> dict[str, Any]:
        """Append a durable note to the current provenance session."""

        return service.note(message)

    @server.resource("panda://workspace")
    def workspace_resource() -> str:
        """Workspace snapshot as JSON."""

        return json.dumps(service.workspace_info(), ensure_ascii=False, indent=2)

    @server.resource("panda://studies")
    def studies_resource() -> str:
        """Study registry as JSON."""

        return json.dumps(service.study_list(), ensure_ascii=False, indent=2)

    return server


def serve(
    root: Path,
    *,
    session_file: Path | None = None,
) -> None:
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
    server = create_server(service)
    try:
        server.run(transport="stdio")
    finally:
        service.close()


__all__ = ["INSTRUCTIONS", "create_server", "serve", "PandaError"]
