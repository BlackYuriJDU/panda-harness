"""CLI behavior: exit codes, JSON contracts, and the fail-closed surface."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from panda_cli.main import _chat_workspace, _reset_terminal, app
from panda_core import credentials
from panda_core.session import SessionLog

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


def test_version() -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.stdout.startswith("panda ")


def test_status_outside_workspace(tmp_path: Path) -> None:
    result = invoke("status", "--root", str(tmp_path), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "config_error"


def test_status_inside_workspace(workspace) -> None:
    result = invoke("status", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["workspace"]["root"] == str(workspace.root)
    assert payload["last_session"] is None


def test_chat_workspace_falls_back_outside_a_corpus(tmp_path: Path) -> None:
    workspace, error = _chat_workspace(tmp_path)
    assert error is not None
    assert error.code == "config_error"
    assert workspace.initialized is False
    assert workspace.root == tmp_path.resolve()


def test_reset_terminal_restores_mouse_and_alt_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = Tty()
    monkeypatch.setattr("sys.stdout", stream)
    _reset_terminal()
    output = stream.getvalue()
    assert "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l" in output
    assert "\x1b[?1049l" in output
    assert "\x1b[?25h" in output


def test_chat_workspace_prefers_an_initialized_corpus(workspace) -> None:
    loaded, error = _chat_workspace(workspace.root)
    assert error is None
    assert loaded.initialized is True
    assert loaded.root == workspace.root


def test_study_list(workspace) -> None:
    result = invoke("study", "list", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [study["id"] for study in payload["studies"]] == ["gse160208", "caso-referencia"]


def test_study_list_track_filter(workspace) -> None:
    result = invoke(
        "study", "list", "--root", str(workspace.root), "--track", "simulated", "--json"
    )
    payload = json.loads(result.stdout)
    assert [study["id"] for study in payload["studies"]] == ["caso-referencia"]


def test_study_show(workspace) -> None:
    result = invoke("study", "show", "gse160208", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["study"]["frontmatter"]["study_id"] == "gse160208"
    assert "Body text" in payload["study"]["body"]


def test_study_show_missing(workspace) -> None:
    result = invoke("study", "show", "missing", "--root", str(workspace.root))
    assert result.exit_code == 2
    assert "study not found" in result.output


def test_policy_check_allow(workspace) -> None:
    result = invoke(
        "policy", "check", "write", "corpus/studies/x.md", "--root", str(workspace.root), "--json"
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["effect"] == "allow"
    assert payload["policy_source"] == "file"


def test_policy_check_deny(workspace) -> None:
    result = invoke(
        "policy", "check", "write", "README.md", "--root", str(workspace.root), "--json"
    )
    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    assert payload["effect"] == "deny"


def test_policy_check_traversal_denied(workspace) -> None:
    result = invoke(
        "policy",
        "check",
        "write",
        "corpus/../README.md",
        "--root",
        str(workspace.root),
        "--json",
    )
    assert result.exit_code == 5
    assert json.loads(result.stdout)["effect"] == "deny"


def test_policy_check_approve_once(workspace) -> None:
    result = invoke(
        "policy",
        "check",
        "shell",
        "cargo install x",
        "--root",
        str(workspace.root),
        "--approve",
        "--json",
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["effect"] == "allow"
    assert payload["approved"] is True


def test_policy_show(workspace) -> None:
    result = invoke("policy", "show", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source"] == "file"
    assert len(payload["policy"]["rules"]) > 10


def test_policy_explain(workspace) -> None:
    result = invoke(
        "policy",
        "explain",
        "shell",
        "git push origin main",
        "--root",
        str(workspace.root),
        "--json",
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["decision"]["effect"] == "ask"
    assert any(rule["id"] == "shell-push" for rule in payload["matching_rules"])


def test_init_dry_run(fake_corpus: Path) -> None:
    result = invoke("init", "--root", str(fake_corpus), "--dry-run", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert not (fake_corpus / ".panda").exists()
    assert not (fake_corpus / "opencode.json").exists()


def test_session_lifecycle(workspace) -> None:
    started = invoke("session", "start", "--root", str(workspace.root), "--json")
    assert started.exit_code == 0
    session_id = json.loads(started.stdout)["session_id"]

    listed = invoke("session", "list", "--root", str(workspace.root), "--json")
    assert listed.exit_code == 0
    assert json.loads(listed.stdout)["sessions"][0]["session_id"] == session_id

    verified = invoke("session", "verify", session_id, "--root", str(workspace.root), "--json")
    assert verified.exit_code == 0
    assert json.loads(verified.stdout)["ok"] is True

    closed = invoke("session", "close", session_id, "--root", str(workspace.root), "--json")
    assert closed.exit_code == 0
    assert json.loads(closed.stdout)["ok"] is True

    verified = invoke("session", "verify", session_id, "--root", str(workspace.root), "--json")
    assert verified.exit_code == 0


def test_session_show(workspace) -> None:
    started = invoke("session", "start", "--root", str(workspace.root), "--json")
    session_id = json.loads(started.stdout)["session_id"]
    shown = invoke("session", "show", session_id, "--root", str(workspace.root), "--json")
    assert shown.exit_code == 0
    payload = json.loads(shown.stdout)
    assert payload["verification"]["ok"] is True
    assert payload["events"][0]["type"] == "session.start"


def test_validate_success(workspace) -> None:
    result = invoke("validate", "--root", str(workspace.root), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["summary"]["passed"] == 10


def test_validate_failure(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANDA_FAKE_FAIL", "numbers")
    result = invoke("validate", "--root", str(workspace.root), "--json")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["summary"]["failed"] == 1


def test_validate_record_creates_closed_session(workspace) -> None:
    result = invoke("validate", "--root", str(workspace.root), "--record", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    session_id = payload["session_id"]
    log = SessionLog.resolve(workspace.sessions_dir, session_id)
    assert log.summary().closed is True
    assert log.verify().ok


def test_run_dry_run(wired_workspace) -> None:
    result = invoke(
        "run", "validate the corpus", "--root", str(wired_workspace.root), "--dry-run"
    )
    assert result.exit_code == 0
    assert "model:" in result.output
    assert "opencode-go/" in result.output
    assert "tools:" in result.output
    assert "run_shell" in result.output
    assert "session:" in result.output


def test_run_requires_task(wired_workspace) -> None:
    result = invoke("run", "--root", str(wired_workspace.root), "--dry-run")
    assert result.exit_code == 2
    assert "task is required" in result.output


@pytest.mark.parametrize(
    "model", ["opencode-go/gpt-6-luna", "opencode-go/gemini-4-argon"]
)
def test_run_plans_every_supported_protocol(wired_workspace, model: str) -> None:
    result = invoke(
        "run",
        "x",
        "--root",
        str(wired_workspace.root),
        "--model",
        model,
        "--dry-run",
    )
    assert result.exit_code == 0
    assert model in result.output


def test_run_rejects_foreign_provider(wired_workspace) -> None:
    result = invoke(
        "run",
        "x",
        "--root",
        str(wired_workspace.root),
        "--model",
        "openrouter/anthropic/claude-sonnet-5.5",
        "--dry-run",
    )
    assert result.exit_code == 2
    assert "not configured" in result.output


def test_models_falls_back_to_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from panda_core.errors import ProviderError
    from panda_core.provider import OpenCodeProvider

    async def unavailable(self):
        raise ProviderError("offline")

    monkeypatch.setattr(OpenCodeProvider, "list_models", unavailable)
    result = invoke("models", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source"] == "snapshot"
    assert payload["count"] == 38
    assert sum(1 for model in payload["models"] if model["supported"]) == 38
    assert any(model["free"] for model in payload["models"])


def test_models_without_api_key_are_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from panda_core.errors import ProviderError
    from panda_core.provider import OpenCodeProvider

    async def unavailable(self):
        raise ProviderError("offline")

    monkeypatch.setattr(OpenCodeProvider, "list_models", unavailable)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    result = invoke("models", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["api_key_env"] == "OPENCODE_API_KEY"
    assert payload["api_key_present"] is False
    assert payload["count"] == 38
    assert sum(1 for model in payload["models"] if model["callable"]) == 38
    assert not any(model["ready"] for model in payload["models"])


def test_models_with_api_key_mark_supported_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from panda_core.errors import ProviderError
    from panda_core.provider import OpenCodeProvider

    async def unavailable(self):
        raise ProviderError("offline")

    monkeypatch.setattr(OpenCodeProvider, "list_models", unavailable)
    monkeypatch.setenv("OPENCODE_API_KEY", "dummy-not-a-secret")
    result = invoke("models", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["api_key_present"] is True
    ready = [model for model in payload["models"] if model["ready"]]
    assert len(ready) == 38
    assert all(model["callable"] for model in ready)
    assert "dummy-not-a-secret" not in result.stdout


def test_models_refresh_hard_fails_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    from panda_core.errors import ProviderError
    from panda_core.provider import OpenCodeProvider

    async def unavailable(self):
        raise ProviderError("offline")

    monkeypatch.setattr(OpenCodeProvider, "list_models", unavailable)
    result = invoke("models", "--refresh", "--json")
    assert result.exit_code == 4


def test_mcp_tools_lists_contract() -> None:
    result = invoke("mcp", "tools")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "panda_validate" in payload["opencode_tools"]
    assert payload["transports"] == ["stdio", "streamable-http"]
    assert payload["resources"] == ["panda://workspace", "panda://studies"]


def test_mcp_serve_rejects_unknown_transport() -> None:
    result = invoke("mcp", "serve", "--transport", "sse")
    assert result.exit_code == 2
    assert "unsupported MCP transport" in result.output


def test_mcp_serve_refuses_non_loopback_binding() -> None:
    result = invoke(
        "mcp", "serve", "--transport", "streamable-http", "--host", "0.0.0.0"
    )
    assert result.exit_code == 2
    assert "loopback-only" in result.output


def test_auth_set_status_clear(tmp_path: Path, isolated_credentials: Path) -> None:
    result = invoke("auth", "set", "--root", str(tmp_path), "--key", "oc_cli_key", "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["provider"] == "opencode-go"
    assert credentials.get_api_key("opencode-go") == "oc_cli_key"

    result = invoke("auth", "status", "--root", str(tmp_path), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source"] == "credential store"
    assert payload["file_exists"] is True
    assert "oc_cli_key" not in result.stdout

    result = invoke("auth", "clear", "--root", str(tmp_path), "--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["removed"] is True
    assert credentials.get_api_key("opencode-go") is None


def test_auth_status_reports_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "env-secret-value")

    result = invoke("auth", "status", "--root", str(tmp_path), "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source"] == "environment"
    assert "env-secret-value" not in result.stdout


def test_auth_set_without_a_key_requires_a_terminal(tmp_path: Path) -> None:
    result = invoke("auth", "set", "--root", str(tmp_path))

    assert result.exit_code == 2
    assert "no API key provided" in result.output
