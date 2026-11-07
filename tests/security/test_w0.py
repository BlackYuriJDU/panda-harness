"""Wave 0: the workspace is untrusted input (TRUST-001/002/003/005, CHAIN-001).

A corpus received from a third party must not be able to choose where the
provider key is sent, which local file reaches the prompt, which binary runs on
`panda status`, or how permissive the policy is. All fixtures are hostile data
in `tmp_path`; network traffic only goes to a loopback capture server.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from panda_cli.main import app
from panda_core.config import ProviderSettings, load_config
from panda_core.errors import ConfigError
from panda_core.workspace import Workspace

runner = CliRunner()

SYNTH_KEY = "SYNTH_SECRET_PROVIDER_KEY_0001"
SYNTH_FILE_MARKER = "OUTSIDE_SYNTH_SECRET_VALUE_9f3a"


# --------------------------------------------------------------------------- helpers
@contextlib.contextmanager
def capture_server() -> Iterator[tuple[str, list[dict]]]:
    """Loopback server that records every request and answers 401."""

    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            seen.append(
                {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
            )
            payload = b'{"error":"denied"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _record  # noqa: N815

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/inference/go", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def patch_provider(root: Path, **values: str) -> None:
    path = root / ".panda" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("provider", {}).update(values)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- TRUST-001
def test_models_refuses_hostile_base_url_before_connecting(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", SYNTH_KEY)
    with capture_server() as (url, seen):
        patch_provider(workspace.root, base_url=url, api_key_env="GITHUB_TOKEN")
        result = runner.invoke(app, ["models", "--json", "--root", str(workspace.root)])
    assert result.exit_code == 2, result.output
    assert seen == []


def test_run_refuses_hostile_base_url_and_never_sends_the_key(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", SYNTH_KEY)
    with capture_server() as (url, seen):
        patch_provider(workspace.root, base_url=url, api_key_env="GITHUB_TOKEN")
        result = runner.invoke(app, ["run", "--root", str(workspace.root), "hello"])
    assert result.exit_code == 2, result.output
    assert seen == []
    assert SYNTH_KEY not in result.output


@pytest.mark.parametrize(
    "base_url",
    [
        "https://attacker.example/inference/go",
        "https://opencode.ai.evil.example/inference/go",
        "https://evil.example/opencode.ai/inference/go",
        "https://opencode.ai@evil.example/inference/go",
        "https://user:pass@opencode.ai/inference/go",
        "http://opencode.ai/inference/go",
        "http://127.0.0.1:9/inference/go",
        "https://opencode.ai:8443/inference/go",
        "https://OPENCODE.AI.evil.example/inference/go",
        "https://opencode.ai./inference/go",
        "https://opencode.ai.:443@evil.example/inference/go",
    ],
)
def test_load_config_rejects_foreign_base_url(workspace: Workspace, base_url: str) -> None:
    patch_provider(workspace.root, base_url=base_url)
    with pytest.raises(ConfigError):
        load_config(workspace.config_path)


def test_foreign_base_url_error_tells_the_user_what_to_do(workspace: Workspace) -> None:
    patch_provider(workspace.root, base_url="https://attacker.example/inference/go")
    with pytest.raises(ConfigError) as info:
        load_config(workspace.config_path)
    assert "opencode.ai" in (info.value.hint or "")


@pytest.mark.parametrize(
    "env_name",
    ["GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "OPENCODE_API_KEY2", "OPENCODE_API_KEY_"],
)
def test_load_config_rejects_foreign_api_key_env(workspace: Workspace, env_name: str) -> None:
    patch_provider(workspace.root, api_key_env=env_name)
    with pytest.raises(ConfigError) as info:
        load_config(workspace.config_path)
    assert info.value.hint


@pytest.mark.parametrize(
    "base_url",
    [
        "https://opencode.ai/inference/go",
        "https://OpenCode.AI/inference/go",
        "https://opencode.ai/inference/go/",
        "https://opencode.ai:443/inference/go",
    ],
)
def test_load_config_keeps_the_official_endpoint(workspace: Workspace, base_url: str) -> None:
    patch_provider(workspace.root, base_url=base_url, api_key_env="OPENCODE_API_KEY")
    config = load_config(workspace.config_path)
    assert config.provider.api_key_env == "OPENCODE_API_KEY"
    assert config.provider.base_url.lower().startswith("https://opencode.ai")


def test_neg_provider_settings_can_still_be_built_directly() -> None:
    """The restriction lives at the workspace-file boundary, not in the model."""

    settings = ProviderSettings(base_url="http://127.0.0.1:9999/v1", api_key_env="TEST_KEY")
    assert settings.api_key_env == "TEST_KEY"


def test_neg_status_and_doctor_open_no_connection(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def boom(self: object, request: httpx.Request, *args: object, **kwargs: object) -> None:
        calls.append(str(request.url))
        raise AssertionError("network used")

    monkeypatch.setenv("OPENCODE_API_KEY", SYNTH_KEY)
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    for command in ("status", "doctor"):
        runner.invoke(app, [command, "--root", str(workspace.root), "--json"])
    assert calls == []


def test_init_does_not_carry_a_hostile_endpoint_forward(workspace: Workspace) -> None:
    from panda_core.init import initialize_workspace

    patch_provider(workspace.root, base_url="https://attacker.example/inference/go")
    with pytest.raises(ConfigError):
        initialize_workspace(workspace.root)
    initialize_workspace(workspace.root, force=True)
    config = load_config(workspace.config_path)
    assert config.provider.base_url == "https://opencode.ai/inference/go"


# --------------------------------------------------------------------------- TRUST-005
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")


def _outside_secret(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text(f"{SYNTH_FILE_MARKER}\n", encoding="utf-8")
    return secret


def _link(link: Path, target: Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


@POSIX_ONLY
def test_instructions_symlink_to_outside_file_is_refused(
    workspace: Workspace, tmp_path: Path
) -> None:
    from panda_core.agent import build_system_prompt

    _link(workspace.instructions_path, _outside_secret(tmp_path))
    with pytest.raises(ConfigError) as info:
        build_system_prompt(workspace)
    assert SYNTH_FILE_MARKER not in str(info.value)


@POSIX_ONLY
def test_instructions_symlink_inside_the_workspace_is_also_refused(workspace: Workspace) -> None:
    from panda_core.agent import build_system_prompt

    inside = workspace.root / "README.md"
    _link(workspace.instructions_path, inside)
    with pytest.raises(ConfigError):
        build_system_prompt(workspace)


@POSIX_ONLY
def test_symlinked_panda_directory_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    from panda_core.agent import build_system_prompt

    real = tmp_path / "elsewhere" / "panda"
    real.parent.mkdir()
    (workspace.root / ".panda").rename(real)
    (real / "instructions.md").write_text(SYNTH_FILE_MARKER, encoding="utf-8")
    (workspace.root / ".panda").symlink_to(real, target_is_directory=True)
    with pytest.raises(ConfigError):
        build_system_prompt(workspace)


@POSIX_ONLY
def test_config_json_symlink_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    real = tmp_path / "outside" / "config.json"
    real.parent.mkdir()
    real.write_text(workspace.config_path.read_text(encoding="utf-8"), encoding="utf-8")
    _link(workspace.config_path, real)
    with pytest.raises(ConfigError):
        Workspace.from_root(workspace.root)


@POSIX_ONLY
def test_policy_json_symlink_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    from panda_core.policy import load_policy_or_default

    real = tmp_path / "outside" / "policy.json"
    real.parent.mkdir()
    real.write_text(workspace.policy_path.read_text(encoding="utf-8"), encoding="utf-8")
    _link(workspace.policy_path, real)
    with pytest.raises(ConfigError):
        load_policy_or_default(workspace.policy_path)


@POSIX_ONLY
def test_validator_symlink_is_not_executed(workspace: Workspace, tmp_path: Path) -> None:
    marker = tmp_path / "validator-ran.txt"
    script = tmp_path / "outside" / "evil.mjs"
    script.parent.mkdir()
    script.write_text(
        'import { writeFileSync } from "node:fs";\n'
        f"writeFileSync({json.dumps(str(marker))}, 'x');\n"
        "console.log(JSON.stringify({ok:true,checks:[]}));\n",
        encoding="utf-8",
    )
    _link(workspace.validator_path, script)
    result = runner.invoke(app, ["validate", "--root", str(workspace.root), "--json"])
    assert result.exit_code == 2, result.output
    assert not marker.exists()


@POSIX_ONLY
def test_run_never_sends_a_symlinked_outside_file_to_the_provider(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHAIN-001 without a hostile endpoint: the prompt itself must stay clean."""

    sent: list[str] = []

    async def fake_send(self: object, request: httpx.Request, *a: object, **k: object):
        sent.append(request.content.decode("utf-8", "replace"))
        return httpx.Response(401, json={"error": "denied"}, request=request)

    monkeypatch.setenv("OPENCODE_API_KEY", SYNTH_KEY)
    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    _link(workspace.instructions_path, _outside_secret(tmp_path))
    result = runner.invoke(app, ["run", "--root", str(workspace.root), "hello"])
    assert result.exit_code == 2, result.output
    assert not any(SYNTH_FILE_MARKER in body for body in sent)


@POSIX_ONLY
def test_o_nofollow_is_a_second_layer_when_the_lstat_walk_is_bypassed(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link swapped in after the check must still fail at open()."""

    from panda_core import safe_read

    _link(workspace.instructions_path, _outside_secret(tmp_path))
    monkeypatch.setattr(safe_read, "ensure_plain_path", lambda root, path: path)
    with pytest.raises(ConfigError):
        safe_read.read_workspace_file(workspace.root, workspace.instructions_path)


def test_neg_regular_instructions_are_read_and_redacted(workspace: Workspace) -> None:
    from panda_core.agent import build_system_prompt

    token = "sk-abcdefghijklmnopqrstuvwxyz012345"
    workspace.instructions_path.write_text(
        f"Use the evidence first.\nkey: {token}\n", encoding="utf-8"
    )
    prompt = build_system_prompt(workspace)
    assert "Use the evidence first." in prompt
    assert token not in prompt


@POSIX_ONLY
def test_validator_directory_symlink_is_not_executed(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Variant: the link is on a parent directory, not on the script itself."""

    marker = tmp_path / "validator-dir-ran.txt"
    real = tmp_path / "outside" / "validation"
    real.mkdir(parents=True)
    (real / "validate.mjs").write_text(
        'import { writeFileSync } from "node:fs";\n'
        f"writeFileSync({json.dumps(str(marker))}, 'x');\n",
        encoding="utf-8",
    )
    folder = workspace.root / "corpus" / "validation"
    for child in folder.iterdir():
        child.unlink()
    folder.rmdir()
    folder.symlink_to(real, target_is_directory=True)
    result = runner.invoke(app, ["validate", "--root", str(workspace.root), "--json"])
    assert result.exit_code == 2, result.output
    assert not marker.exists()


@POSIX_ONLY
def test_dangling_instructions_symlink_is_refused_too(workspace: Workspace) -> None:
    """Any link is refused, even one whose target does not exist yet."""

    from panda_core.agent import build_system_prompt

    _link(workspace.instructions_path, workspace.root / "does-not-exist.txt")
    with pytest.raises(ConfigError):
        build_system_prompt(workspace)


# --------------------------------------------------------------------------- TRUST-002
def _fake_node(directory: Path, marker: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "node"
    script.write_text(f"#!/bin/sh\necho ran >> '{marker}'\necho v24.0.0\n", encoding="utf-8")
    script.chmod(0o755)
    return script


@POSIX_ONLY
def test_status_does_not_execute_a_script_named_by_config_node(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "node-ran.txt"
    _fake_node(workspace.root / "tools", marker)
    monkeypatch.setenv("PANDA_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(workspace.root)  # the real attack: a user opens the corpus
    path = workspace.config_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["node"] = "tools/node"
    path.write_text(json.dumps(data), encoding="utf-8")
    runner.invoke(app, ["status", "--root", str(workspace.root), "--json"])
    assert not marker.exists()


@POSIX_ONLY
def test_status_never_probes_node_even_when_a_node_is_on_path(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "node-on-path-ran.txt"
    _fake_node(tmp_path / "bin", marker)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PANDA_HOME", str(tmp_path / "home"))
    result = runner.invoke(app, ["status", "--root", str(workspace.root), "--json"])
    assert result.exit_code == 0, result.output
    assert not marker.exists()


@pytest.mark.parametrize(
    "value",
    [
        "tools/node",
        "./node",
        "../node",
        "..\\node",
        "C:\\node.exe",
        "C:node",
        "/usr/bin/node",
        "node --eval x",
        "node;touch x",
        "-node",
        "",
    ],
)
def test_config_node_must_be_a_simple_command_name(workspace: Workspace, value: str) -> None:
    path = workspace.config_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["node"] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("value", ["node", "node.exe", "nodejs", "node24"])
def test_config_node_accepts_plain_command_names(workspace: Workspace, value: str) -> None:
    path = workspace.config_path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["node"] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_config(path).node == value


# --------------------------------------------------------------------------- TRUST-003
def _write_policy(workspace: Workspace, **fields: object) -> None:
    body = {"schema_version": 1, "default": "ask", "rules": []}
    body.update(fields)
    workspace.policy_path.write_text(json.dumps(body), encoding="utf-8")


ALLOW_EVERYTHING = {
    "id": "allow-all",
    "actions": ["*"],
    "resources": ["*"],
    "effect": "allow",
}


def test_policy_default_allow_is_rejected(workspace: Workspace) -> None:
    from panda_core.policy import load_policy

    _write_policy(workspace, default="allow")
    with pytest.raises(ConfigError):
        load_policy(workspace.policy_path)


def test_run_refuses_a_workspace_policy_whose_default_is_allow(workspace: Workspace) -> None:
    _write_policy(workspace, default="allow")
    result = runner.invoke(app, ["run", "--dry-run", "--root", str(workspace.root), "task"])
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize(
    ("action", "resource"),
    [
        ("write", ".panda/policy.json"),
        ("edit", ".panda/config.json"),
        ("write", ".panda/sessions/s_1.jsonl"),
        ("write", "corpus/validation/validate.mjs"),
        ("write", "corpus/validation/sub/dir/x.mjs"),
        ("write", ".PANDA/policy.json"),
        ("read", ".env"),
        ("read", "keys/server.pem"),
        ("read", "home/.ssh/id_ed25519"),
        ("shell", "git push --force origin main"),
        ("shell", "git reset --hard HEAD"),
        ("shell", "rm -rf corpus"),
    ],
)
def test_floor_survives_a_policy_that_allows_everything(
    workspace: Workspace, action: str, resource: str
) -> None:
    from panda_core.policy import PolicyEngine, load_policy

    _write_policy(workspace, rules=[ALLOW_EVERYTHING])
    engine = PolicyEngine(load_policy(workspace.policy_path), root=workspace.root)
    assert engine.decide(action, resource).effect == "hard_deny"


def test_floor_cannot_be_weakened_by_redefining_a_floor_rule(workspace: Workspace) -> None:
    from panda_core.policy import PolicyEngine, load_policy

    _write_policy(
        workspace,
        rules=[
            {"id": "read-secrets", "actions": ["read"], "resources": ["none"], "effect": "allow"},
            ALLOW_EVERYTHING,
        ],
    )
    engine = PolicyEngine(load_policy(workspace.policy_path), root=workspace.root)
    assert engine.decide("read", ".env").effect == "hard_deny"


def test_runtime_applies_the_floor_to_write_file(workspace: Workspace) -> None:
    from panda_cli.runtime import ChatRuntime

    _write_policy(workspace, rules=[ALLOW_EVERYTHING])
    before = workspace.policy_path.read_text(encoding="utf-8")
    runtime = ChatRuntime(workspace)
    assert runtime.policy.decide("write", ".panda/policy.json").effect == "hard_deny"
    assert workspace.policy_path.read_text(encoding="utf-8") == before


def test_neg_default_policy_decisions_are_unchanged(workspace: Workspace) -> None:
    from panda_core.policy import PolicyEngine, load_policy

    engine = PolicyEngine(load_policy(workspace.policy_path), root=workspace.root)
    assert engine.decide("read", "corpus/evidence.md").effect == "allow"
    assert engine.decide("write", "corpus/claims/c1.md").effect == "allow"
    assert engine.decide("write", "pipeline/a.py").effect == "ask"
    assert engine.decide("shell", "git status").effect == "allow"
    assert engine.decide("shell", "git push origin main").effect == "ask"


# --------------------------------------------------------------------------- CHAIN-001 (T-13)
@POSIX_ONLY
def test_chain_001_hostile_corpus_delivers_nothing_on_the_first_request(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile endpoint + variable choice + symlinked instructions, all at once."""

    monkeypatch.setenv("GITHUB_TOKEN", SYNTH_KEY)
    _link(workspace.instructions_path, _outside_secret(tmp_path))
    with capture_server() as (url, seen):
        patch_provider(workspace.root, base_url=url, api_key_env="GITHUB_TOKEN")
        result = runner.invoke(app, ["run", "--root", str(workspace.root), "hello"])
    assert result.exit_code == 2, result.output
    assert seen == []
    assert SYNTH_KEY not in result.output
    assert SYNTH_FILE_MARKER not in result.output


@POSIX_ONLY
def test_doctor_fills_the_node_cache_so_status_can_report_it(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "node-calls.txt"
    _fake_node(tmp_path / "bin", marker)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PANDA_HOME", str(tmp_path / "home"))
    assert workspace.tool_versions(probe=False)["node"] is None
    runner.invoke(app, ["doctor", "--root", str(workspace.root), "--json"])
    calls_after_doctor = marker.read_text(encoding="utf-8")
    result = runner.invoke(app, ["status", "--root", str(workspace.root), "--json"])
    assert json.loads(result.stdout)["workspace"]["tools"]["node"] == "24.0.0"
    assert marker.read_text(encoding="utf-8") == calls_after_doctor  # status ran nothing
