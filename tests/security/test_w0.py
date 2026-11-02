"""Wave 0: the workspace is untrusted input (TRUST-001/002/003/005, CHAIN-001).

A corpus received from a third party must not be able to choose where the
provider key is sent, which local file reaches the prompt, which binary runs on
`panda status`, or how permissive the policy is. All fixtures are hostile data
in `tmp_path`; network traffic only goes to a loopback capture server.
"""

from __future__ import annotations

import contextlib
import json
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
