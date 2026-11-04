"""Trilha B — MCP HTTP (loopback): Host/Origin (DNS rebinding) e paridade de política."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("mcp")

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "poc", "version": "0"}},
}
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def server(workspace, tmp_path: Path):  # type: ignore[no-untyped-def]
    port = _free_port()
    env = dict(os.environ, HOME=str(tmp_path / "home"), PANDA_HOME=str(tmp_path / "ph"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "panda_cli", "mcp", "serve", "--root", str(workspace.root),
         "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp", port
    proc.terminate()
    proc.wait(timeout=10)


def test_mcp_http_rejects_rebound_host_and_foreign_origin(server) -> None:  # type: ignore[no-untyped-def]
    url, port = server
    ok = httpx.post(url, json=INIT, headers=HEADERS, timeout=10)
    evil_host = httpx.post(url, json=INIT, headers={**HEADERS, "Host": f"evil.example:{port}"},
                           timeout=10)
    evil_origin = httpx.post(url, json=INIT,
                             headers={**HEADERS, "Origin": "http://evil.example"}, timeout=10)
    text_plain = httpx.post(url, content=json.dumps(INIT),
                            headers={"Content-Type": "text/plain", "Accept": HEADERS["Accept"]},
                            timeout=10)
    assert ok.status_code == 200, ok.text[:200]
    assert evil_host.status_code in {400, 403, 421}, evil_host.status_code
    assert evil_origin.status_code in {400, 403}, evil_origin.status_code
    assert text_plain.status_code in {400, 415}, text_plain.status_code


@pytest.fixture()
def slow_server(workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PANDA_FAKE_SLEEP", "3000")  # o validador fixture dorme 3 s
    yield from server.__wrapped__(workspace, tmp_path)  # type: ignore[attr-defined]


def _session(url: str) -> dict[str, str]:
    first = httpx.post(url, json=INIT, headers=HEADERS, timeout=10)
    sid = first.headers["mcp-session-id"]
    headers = {**HEADERS, "mcp-session-id": sid}
    httpx.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers=headers, timeout=10)
    return headers


def test_long_validate_does_not_freeze_other_mcp_clients(slow_server) -> None:  # type: ignore[no-untyped-def]
    import threading

    url, _ = slow_server
    headers = _session(url)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "validate", "arguments": {}}}
    worker = threading.Thread(
        target=lambda: httpx.post(url, json=call, headers=headers, timeout=30), daemon=True
    )
    worker.start()
    time.sleep(0.5)
    started = time.monotonic()
    httpx.post(url, json=INIT, headers=HEADERS, timeout=30)  # outro cliente
    waited = time.monotonic() - started
    worker.join(timeout=30)
    assert waited < 1.0, f"outro cliente esperou {waited:.1f}s pelo validate"
