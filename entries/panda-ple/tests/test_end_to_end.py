"""The whole entry over the real serve protocol: taskbridge server, WebSocket
evaluator, local task container, and a scripted model.

These mirror the `kit check` stages: smoke (write hello.txt), MCP (call the
app's tool and write its answer), and cancel (send `done` within 10 s).
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import httpx
import pytest
from panda_core.provider import OpenCodeProvider

import harness
import taskbridge
from tests.fakes import LocalContainer, McpApp, evaluate

SMOKE = (
    "Create a file named `hello.txt` in the current working directory whose entire"
    " content is the single line:\n\nHELLO\n\nProject codename ZEBRA-4417. Reply done."
)
MCP_TASK = (
    "The task's MCP server `apps` has a tool named `secret_word`. Call it, then create"
    " `mcp.txt` in the working directory whose entire content is the word it returns."
)


def chat_stream(*, text: str = "", call: tuple[str, dict[str, Any]] | None = None) -> str:
    chunks: list[dict[str, Any]] = []
    if text:
        chunks.append({"choices": [{"delta": {"content": text}, "finish_reason": None}]})
    if call:
        name, arguments = call
        chunks.append(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_{name}",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
    reason = "tool_calls" if call else "stop"
    chunks.append({"choices": [{"delta": {}, "finish_reason": reason}]})
    chunks.append({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def scripted_model(request: httpx.Request) -> httpx.Response:
    """A stand-in model that solves the kit's smoke and MCP tasks step by step."""

    payload = json.loads(request.content)
    assert "temperature" in payload  # glm on chat: sampling allowed
    messages = payload["messages"]
    tools = {tool["function"]["name"] for tool in payload.get("tools", [])}
    results = [m["content"] for m in messages if m["role"] == "tool"]
    task = next(m["content"] for m in messages if m["role"] == "user")
    if "secret_word" in task:
        if not results:
            assert "mcp__apps__secret_word" in tools
            body = chat_stream(call=("mcp__apps__secret_word", {}))
        elif len(results) == 1:
            body = chat_stream(call=("write_file", {"path": "mcp.txt", "content": results[0]}))
        else:
            body = chat_stream(text="Wrote mcp.txt with the secret word.")
    elif not results:
        body = chat_stream(call=("write_file", {"path": "hello.txt", "content": "HELLO\n"}))
    else:
        body = chat_stream(text="done: wrote hello.txt")
    return httpx.Response(200, text=body)


@pytest.fixture()
def gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_BASE_URL", "http://model.test/v1")
    monkeypatch.setenv("MODEL_NAME", "glm-5.2")
    monkeypatch.setenv("MODEL_API_KEY", "test-key")
    monkeypatch.setenv("PANDA_PLE_BACKUP_ROOT", str(tmp_path / "backup"))
    handler: list[Any] = [scripted_model]

    def factory(settings, model):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: handler[0](r)))
        return OpenCodeProvider(settings, model=model, client=client, api_key="test-key")

    monkeypatch.setattr(harness, "PROVIDER_FACTORY", factory)
    return handler


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def run_task(container: LocalContainer, instruction: str, **kwargs) -> dict[str, Any]:
    port = free_port()
    server = asyncio.create_task(taskbridge.serve_forever(harness.solve, port))
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.05)
        return await asyncio.wait_for(
            evaluate(f"ws://127.0.0.1:{port}", container, instruction, **kwargs), 60
        )
    finally:
        server.cancel()


def test_smoke_task_writes_hello_and_finishes(gateway, tmp_path) -> None:
    container = LocalContainer(tmp_path / "app")
    container.root.mkdir()
    outcome = asyncio.run(run_task(container, SMOKE))
    assert outcome["end"]["type"] == "done"
    assert (container.root / "hello.txt").read_text() == "HELLO\n"
    assert "hello.txt" in outcome["end"]["final_message"]
    assert outcome["usage"] and outcome["usage"][0]["model"] == "glm-5.2"
    assert outcome["usage"][0]["input_tokens"] == 100
    # Metadata-only logs: the task's text never reaches the evaluator's log.
    assert outcome["logs"] and not any("ZEBRA-4417" in line for line in outcome["logs"])


def test_mcp_task_uses_the_app_tool(gateway, tmp_path) -> None:
    app = McpApp()
    container = LocalContainer(tmp_path / "app", mcp=app)
    container.root.mkdir()
    outcome = asyncio.run(run_task(container, MCP_TASK, mcp=True))
    assert outcome["end"]["type"] == "done"
    assert app.calls == ["secret_word"]
    assert (container.root / "mcp.txt").read_text() == app.word


def test_cancel_sends_done_with_a_summary_quickly(gateway, tmp_path) -> None:
    calls = {"n": 0}

    async def slow_after_first_write(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return scripted_model(request)
        await asyncio.sleep(60)  # the model "thinks" past the deadline
        return scripted_model(request)

    gateway[0] = slow_after_first_write
    container = LocalContainer(tmp_path / "app")
    container.root.mkdir()
    outcome = asyncio.run(run_task(container, SMOKE, cancel_after=2.0))
    assert outcome["end"]["type"] == "done"
    assert outcome["seconds_after_cancel"] is not None
    assert outcome["seconds_after_cancel"] < 10
    assert "hello.txt" in outcome["end"]["final_message"]


def test_failure_before_any_model_output_is_an_error(gateway, tmp_path) -> None:
    gateway[0] = lambda request: httpx.Response(401, text="bad key")
    container = LocalContainer(tmp_path / "app")
    container.root.mkdir()
    outcome = asyncio.run(run_task(container, SMOKE))
    # Not started: the evaluator may run the task again.
    assert outcome["end"]["type"] == "error"
