"""Tool registry: path containment, policy, approval, and biomedical reads."""

from __future__ import annotations

import asyncio
import json
import sys

from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.session import SessionLog
from panda_core.tools import ApprovalRequest, ToolContext, ToolRegistry


def make_registry(workspace, *, approver=None) -> tuple[ToolRegistry, SessionLog]:
    session = SessionLog.create(workspace.sessions_dir, label="tools-test")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
        approve=approver,
    )
    return ToolRegistry(ctx), session


def call(registry: ToolRegistry, name: str, **arguments):
    return asyncio.run(registry.call(name, arguments))


def test_specs_are_openai_ready(workspace) -> None:
    registry, _ = make_registry(workspace)
    specs = registry.to_openai()
    assert {spec["function"]["name"] for spec in specs} >= {
        "read_file",
        "write_file",
        "run_shell",
        "validate",
        "evidence_list",
    }
    assert all(spec["type"] == "function" for spec in specs)
    assert all("parameters" in spec["function"] for spec in specs)


def test_read_file_returns_numbered_lines(workspace) -> None:
    registry, _ = make_registry(workspace)
    result = call(registry, "read_file", path="README.md")
    assert result.ok
    assert "README.md:1-1 of 1 lines" in result.content
    assert result.data["path"] == "README.md"


def test_read_file_refuses_secrets(workspace) -> None:
    (workspace.root / ".env").write_text("TOKEN=do-not-read", encoding="utf-8")
    registry, _ = make_registry(workspace)
    result = call(registry, "read_file", path=".env")
    assert not result.ok
    assert result.blocked
    assert "read-secrets" in result.content


def test_path_escape_is_rejected(workspace) -> None:
    registry, _ = make_registry(workspace)
    result = call(registry, "read_file", path="../outside.txt")
    assert not result.ok
    assert "escapes the workspace" in result.content


def test_unknown_tool_and_missing_arguments(workspace) -> None:
    registry, _ = make_registry(workspace)
    unknown = call(registry, "nope")
    assert not unknown.ok
    assert "unknown tool" in unknown.content
    missing = call(registry, "read_file")
    assert not missing.ok
    assert "missing required argument" in missing.content


def test_write_defaults_to_deny(workspace) -> None:
    registry, _ = make_registry(workspace)
    result = call(registry, "write_file", path="README.md", content="overwritten")
    assert not result.ok
    assert result.blocked
    assert (workspace.root / "README.md").read_text(encoding="utf-8") == "# fixture corpus\n"


def test_write_inside_corpus_scope_is_allowed(workspace) -> None:
    registry, _ = make_registry(workspace)
    payload = "---\nclaim_id: claim-a\nstudy_id: gse160208\nstatus: draft\n---\n\nClaim body.\n"
    result = call(registry, "write_file", path="corpus/claims/claim-a.md", content=payload)
    assert result.ok, result.content
    assert result.data["created"]
    assert (workspace.root / "corpus/claims/claim-a.md").read_text(encoding="utf-8") == payload


def test_edit_file_requires_unique_match(workspace) -> None:
    (workspace.root / "corpus/notes.md").write_text("alpha beta alpha\n", encoding="utf-8")
    registry, _ = make_registry(workspace)
    ambiguous = call(
        registry, "edit_file", path="corpus/notes.md", old_string="alpha", new_string="gamma"
    )
    assert not ambiguous.ok
    assert "appears 2 times" in ambiguous.content
    replaced = call(
        registry,
        "edit_file",
        path="corpus/notes.md",
        old_string="alpha",
        new_string="gamma",
        replace_all=True,
    )
    assert replaced.ok, replaced.content
    assert (workspace.root / "corpus/notes.md").read_text(encoding="utf-8") == "gamma beta gamma\n"


def test_shell_requires_approval(workspace) -> None:
    registry, _ = make_registry(workspace)
    blocked = call(registry, "run_shell", argv=[sys.executable, "-c", "print(1)"])
    assert blocked.blocked
    assert "approval is required" in blocked.content


def test_shell_runs_only_when_approved(workspace) -> None:
    seen: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> bool:
        seen.append(request)
        return True

    registry, _ = make_registry(workspace, approver=approve)
    result = call(registry, "run_shell", argv=[sys.executable, "-c", "print('allowed')"])
    assert result.ok, result.content
    assert "allowed" in result.data["stdout"]
    assert seen and seen[0].tool == "run_shell"
    assert seen[0].preview_kind == "command"


def test_shell_denied_by_operator_does_not_execute(workspace) -> None:
    async def refuse(request: ApprovalRequest) -> bool:
        return False

    registry, _ = make_registry(workspace, approver=refuse)
    result = call(registry, "run_shell", argv=[sys.executable, "-c", "print('nope')"])
    assert result.blocked
    assert "denied by the operator" in result.content


def test_biomedical_reads_from_corpus(workspace) -> None:
    corpus = workspace.root / "corpus"
    (corpus / "evidence").mkdir(parents=True, exist_ok=True)
    evidence = [
        {"evidence_id": "ev-1", "study_id": "gse160208", "license": "GEO"},
        {"evidence_id": "ev-2", "study_id": "caso-referencia", "license": "project"},
    ]
    (corpus / "evidence" / "evidence.jsonl").write_text(
        "\n".join(json.dumps(item) for item in evidence) + "\n", encoding="utf-8"
    )
    (corpus / "numbers.json").write_text(
        json.dumps({"numbers": [{"number_id": "n-1", "value": "0.42", "unit": "fraction"}]}),
        encoding="utf-8",
    )
    claims = corpus / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / "claim-a.md").write_text(
        "---\nclaim_id: claim-a\nstudy_id: gse160208\nstatus: draft\n---\n\nBody.\n",
        encoding="utf-8",
    )
    registry, _ = make_registry(workspace)

    listed = call(registry, "evidence_list", study_id="gse160208")
    assert listed.ok, listed.content
    assert [item["evidence_id"] for item in listed.data["evidence"]] == ["ev-1"]

    shown = call(registry, "evidence_show", evidence_id="ev-2")
    assert shown.ok
    assert shown.data["evidence"]["study_id"] == "caso-referencia"
    assert not call(registry, "evidence_show", evidence_id="ev-9").ok

    numbers = call(registry, "number_list")
    assert numbers.ok
    assert numbers.data["numbers"][0]["numeric"] == 0.42

    claim = call(registry, "claim_show", claim_id="claim-a")
    assert claim.ok
    assert "Body." in claim.data["claim"]["body"]


def test_workspace_info_reports_provider_without_secret(workspace, monkeypatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-should-never-appear")
    registry, _ = make_registry(workspace)
    result = call(registry, "workspace_info")
    assert result.ok
    assert result.data["provider"]["api_key_present"] is True
    assert "sk-should-never-appear" not in result.content
    assert result.data["provider"]["base_url"] == "https://opencode.ai/inference"


def test_grep_skips_vcs_and_reports_lines(workspace) -> None:
    registry, _ = make_registry(workspace)
    result = call(registry, "grep", pattern="fixture corpus")
    assert result.ok
    assert any(match.startswith("README.md:1:") for match in result.data["matches"])
