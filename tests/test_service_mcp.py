"""Service layer and MCP adapter: same policy, same provenance, same contract."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from panda_core.errors import ConfigError
from panda_core.mcp_server import create_server
from panda_core.service import PandaService
from panda_core.session import SessionLog
from panda_core.workspace import Workspace


@pytest.fixture()
def service(workspace: Workspace) -> PandaService:
    return PandaService(workspace, autocreate_session=False)


@pytest.fixture()
def recording_service(workspace: Workspace) -> PandaService:
    session = SessionLog.create(workspace.sessions_dir, label="service-test")
    return PandaService(workspace, session=session)


def structured(result: Any) -> Any:
    """Unwrap FastMCP's ``(content_blocks, structured)`` result."""

    payload = result[1] if isinstance(result, tuple) else result
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


def test_workspace_info(service: PandaService) -> None:
    info = service.workspace_info()
    assert info["root"] == str(service.workspace.root)
    assert info["initialized"] is True
    assert info["validator_exists"] is True
    assert info["policy"]["source"] == "file"
    assert info["policy"]["rules"] > 0
    assert info["session_id"] is None


def test_validate_reports_and_records(recording_service: PandaService) -> None:
    payload = recording_service.validate()
    assert payload["ok"] is True
    assert payload["summary"]["passed"] == 10
    assert payload["session_logged"] is True
    types = [
        json.loads(line)["type"]
        for line in recording_service.session.path.read_text(encoding="utf-8").splitlines()
    ]
    assert "tool.call" in types
    assert "validate.result" in types


def test_validate_works_without_session(service: PandaService) -> None:
    payload = service.validate()
    assert payload["ok"] is True
    assert payload["session_logged"] is False


def test_policy_check_matches_cli_semantics(service: PandaService) -> None:
    allowed = service.policy_check("write", "corpus/studies/x.md")
    assert allowed["effect"] == "allow"
    assert allowed["policy_source"] == "file"

    denied = service.policy_check("write", "README.md")
    assert denied["effect"] == "deny"

    traversal = service.policy_check("write", "corpus/../README.md")
    assert traversal["effect"] == "deny"

    approved = service.policy_check("shell", "cargo install x", approved=True)
    assert approved["effect"] == "allow"
    assert approved["approved"] is True


def test_study_tools(service: PandaService) -> None:
    studies = service.study_list()
    assert [study["id"] for study in studies] == ["gse160208", "caso-referencia"]
    assert service.study_list(track="simulated")[0]["id"] == "caso-referencia"
    card = service.study_show("gse160208")
    assert card["frontmatter"]["study_id"] == "gse160208"
    assert "Body text" in card["body"]
    with pytest.raises(ConfigError):
        service.study_show("missing")


def test_note_requires_a_session(service: PandaService) -> None:
    payload = service.note("observation")
    assert payload["recorded"] is False
    assert payload["session_id"] is None


def test_note_is_truncated(recording_service: PandaService) -> None:
    payload = recording_service.note("x" * 5000)
    assert payload["recorded"] is True
    assert payload["message_bytes"] > 4000
    last = json.loads(
        recording_service.session.path.read_text(encoding="utf-8").splitlines()[-1]
    )
    assert last["data"]["message"].endswith("[truncated]")


def test_note_rejects_empty(recording_service: PandaService) -> None:
    with pytest.raises(ValueError):
        recording_service.note("   ")


def test_autocreate_session_and_close(workspace: Workspace) -> None:
    service = PandaService(workspace, autocreate_session=True)
    service.note("first")
    assert service.session is not None
    assert service.owns_session is True
    service.close()
    assert service.session.is_closed()


def test_mcp_tool_surface(workspace: Workspace) -> None:
    pytest.importorskip("mcp.server.fastmcp")
    server = create_server(PandaService(workspace, autocreate_session=False))

    async def scenario() -> None:
        tools = {tool.name for tool in await server.list_tools()}
        assert tools == {
            "workspace_info",
            "validate",
            "policy_check",
            "study_list",
            "study_show",
            "session_note",
        }

        info = structured(await server.call_tool("workspace_info", {}))
        assert info["root"] == str(workspace.root)

        studies = structured(await server.call_tool("study_list", {}))
        assert [study["id"] for study in studies] == ["gse160208", "caso-referencia"]

        card = structured(
            await server.call_tool("study_show", {"study_id": "gse160208"})
        )
        assert card["frontmatter"]["track"] == "public"

        denied = structured(
            await server.call_tool(
                "policy_check", {"action": "write", "resource": "README.md"}
            )
        )
        assert denied["effect"] == "deny"

        report = structured(await server.call_tool("validate", {}))
        assert report["ok"] is True
        assert report["summary"]["passed"] == 10

    asyncio.run(scenario())


def test_mcp_session_note_is_logged(workspace: Workspace) -> None:
    pytest.importorskip("mcp.server.fastmcp")
    session = SessionLog.create(workspace.sessions_dir, label="mcp-note")
    server = create_server(PandaService(workspace, session=session))

    async def scenario() -> None:
        payload = structured(
            await server.call_tool("session_note", {"message": "mcp observation"})
        )
        assert payload["recorded"] is True
        assert payload["session_id"] == session.session_id

    asyncio.run(scenario())
    assert session.verify().ok


def test_mcp_resources_are_json(workspace: Workspace) -> None:
    pytest.importorskip("mcp.server.fastmcp")
    server = create_server(PandaService(workspace, autocreate_session=False))

    async def scenario() -> None:
        names = {str(resource.uri) for resource in await server.list_resources()}
        assert names == {"panda://workspace", "panda://studies"}

        workspace_payload = json.loads((await server.read_resource("panda://workspace"))[0].content)
        assert workspace_payload["root"] == str(workspace.root)

        studies_payload = json.loads((await server.read_resource("panda://studies"))[0].content)
        assert len(studies_payload) == 2

    asyncio.run(scenario())


def test_service_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        PandaService.from_env(tmp_path)
