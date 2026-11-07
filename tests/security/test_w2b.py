"""Wave 2B: processes, providers, adapters, byte-exact edits and content hashes.

SEC-006/008, PROV-002/003/004, ADP-001/002, EDIT-001, LED-001. Synthetic data only;
providers are fixtures, never the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.process import run_command
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="process groups need POSIX")


def running(marker: str) -> list[int]:
    """PIDs whose command line contains `marker` (Linux /proc)."""

    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if marker in cmdline and int(entry.name) != os.getpid():
            pids.append(int(entry.name))
    return pids


def wait_gone(marker: str, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not running(marker):
            return True
        time.sleep(0.05)
    return not running(marker)


async def approve_all(request) -> bool:
    return True


def make_registry(workspace, *, approver=approve_all):
    session = SessionLog.create(workspace.sessions_dir, label="w2b")
    policy, _ = load_policy_or_default(workspace.policy_path)
    ctx = ToolContext(
        workspace=workspace,
        policy=PolicyEngine(policy, root=workspace.root),
        session=session,
        approve=approver,
    )
    return ToolRegistry(ctx), session


# --------------------------------------------------------------------------- SEC-008
@POSIX
def test_timeout_kills_the_whole_process_group() -> None:
    started = time.monotonic()
    result = run_command(["sh", "-c", "sleep 31.7101 & sleep 31.7101"], timeout=1)
    assert result.timed_out and result.returncode == 124
    assert wait_gone("31.7101", seconds=10), "a grandchild survived the timeout"
    assert time.monotonic() - started < 15, "cleanup took too long under load"


@POSIX
def test_a_background_child_does_not_outlive_a_finished_command() -> None:
    result = run_command(["sh", "-c", "sleep 31.7102 & echo done"], timeout=10)
    assert result.ok and "done" in result.stdout
    assert wait_gone("31.7102", seconds=10), "background child survived under load"


@POSIX
def test_output_is_capped_and_the_command_is_stopped() -> None:
    code = "import sys\nfor _ in range(400):\n    sys.stdout.write('x' * 50000)\n"
    started = time.monotonic()
    result = run_command([sys.executable, "-c", code], timeout=30, max_output_bytes=100_000)
    assert result.output_truncated
    assert len(result.stdout.encode()) <= 100_000
    assert time.monotonic() - started < 20, "output capping took too long under load"


@POSIX
def test_stderr_is_capped_too() -> None:
    code = "import sys\nfor _ in range(400):\n    sys.stderr.write('e' * 50000)\n"
    result = run_command([sys.executable, "-c", code], timeout=30, max_output_bytes=100_000)
    assert result.output_truncated and len(result.stderr.encode()) <= 100_000


@POSIX
def test_a_cancel_event_stops_a_running_command() -> None:
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    result = run_command(["sleep", "31.7103"], timeout=60, cancel=cancel)
    assert result.cancelled
    assert wait_gone("31.7103", seconds=10)
    assert time.monotonic() - started < 10, "cancellation took too long under load"


@POSIX
def test_cancelling_the_tool_call_stops_the_command(workspace) -> None:
    registry, _ = make_registry(workspace)

    async def scenario() -> float:
        task = asyncio.create_task(
            registry.call("run_shell", {"argv": ["sleep", "31.7104"], "timeout_seconds": 600})
        )
        # active wait: poll until the process is actually running before cancelling
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if running("31.7104"):
                break
            await asyncio.sleep(0.05)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 10, "cancellation took too long under load"
    assert wait_gone("31.7104", seconds=10)


@POSIX
def test_wait_for_really_bounds_a_running_tool(workspace) -> None:
    registry, _ = make_registry(workspace)

    async def scenario() -> float:
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                registry.call("run_shell", {"argv": ["sleep", "31.7105"], "timeout_seconds": 600}),
                timeout=2.0,
            )
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 10, "wait_for bound took too long under load"
    assert wait_gone("31.7105", seconds=10)


def test_neg_output_exit_code_and_missing_binary_are_unchanged() -> None:
    ok = run_command([sys.executable, "-c", "print('olá'); import sys; sys.exit(3)"], timeout=10)
    assert ok.returncode == 3 and ok.stdout.strip() == "olá" and not ok.timed_out
    assert run_command(["definitely-not-a-binary-xyz"]).returncode == 127


@POSIX
def test_stdin_is_closed_so_a_reading_command_does_not_hang() -> None:
    started = time.monotonic()
    result = run_command(["cat"], timeout=10)
    assert result.ok and result.stdout == "" and time.monotonic() - started < 3


# --------------------------------------------------------------------------- EDIT-001
def call(registry, name: str, **arguments):
    return asyncio.run(registry.call(name, arguments))


def edit(registry, path: str, old: str, new: str, **extra):
    return call(registry, "edit_file", path=path, old_string=old, new_string=new, **extra)


def test_edit_keeps_crlf_line_endings(workspace) -> None:
    target = workspace.root / "corpus" / "crlf.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    registry, _ = make_registry(workspace)
    assert edit(registry, "corpus/crlf.txt", "beta", "BETA").ok
    assert target.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_edit_matches_a_multiline_old_string_in_a_crlf_file(workspace) -> None:
    target = workspace.root / "corpus" / "crlf2.txt"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    registry, _ = make_registry(workspace)
    assert edit(registry, "corpus/crlf2.txt", "one\ntwo", "ONE\nTWO\nextra").ok
    assert target.read_bytes() == b"ONE\r\nTWO\r\nextra\r\nthree\r\n"


def test_edit_replace_all_keeps_crlf(workspace) -> None:
    target = workspace.root / "corpus" / "crlf3.txt"
    target.write_bytes(b"x\r\nx\r\n")
    registry, _ = make_registry(workspace)
    assert edit(registry, "corpus/crlf3.txt", "x", "y", replace_all=True).ok
    assert target.read_bytes() == b"y\r\ny\r\n"


def test_edit_refuses_a_file_that_is_not_utf8_and_leaves_it_alone(workspace) -> None:
    target = workspace.root / "corpus" / "lat1.txt"
    target.write_bytes(b"caf\xe9 alpha\nzeta\n")
    registry, _ = make_registry(workspace)
    result = edit(registry, "corpus/lat1.txt", "zeta", "ZETA")
    assert not result.ok and "UTF-8" in result.content
    assert target.read_bytes() == b"caf\xe9 alpha\nzeta\n"
    assert b"\xef\xbf\xbd" not in target.read_bytes()


def test_edit_refuses_mixed_line_endings_and_leaves_the_file_alone(workspace) -> None:
    target = workspace.root / "corpus" / "mixed.txt"
    target.write_bytes(b"a\r\nb\nc\r\n")
    registry, _ = make_registry(workspace)
    result = edit(registry, "corpus/mixed.txt", "b", "B")
    assert not result.ok and "line ending" in result.content
    assert target.read_bytes() == b"a\r\nb\nc\r\n"


def test_edit_preserves_a_bom_non_ascii_and_a_missing_final_newline(workspace) -> None:
    target = workspace.root / "corpus" / "bom.txt"
    target.write_bytes("﻿çafé 日本\nfim".encode())
    registry, _ = make_registry(workspace)
    assert edit(registry, "corpus/bom.txt", "fim", "FIM").ok
    assert target.read_bytes() == "﻿çafé 日本\nFIM".encode()


def test_neg_lf_files_and_write_file_are_unchanged(workspace) -> None:
    registry, _ = make_registry(workspace)
    lf = workspace.root / "corpus" / "lf.txt"
    lf.write_bytes(b"a\nb\n")
    assert edit(registry, "corpus/lf.txt", "b", "B").ok and lf.read_bytes() == b"a\nB\n"
    assert call(registry, "write_file", path="corpus/w.txt", content="a\r\nb\r\n").ok
    assert (workspace.root / "corpus" / "w.txt").read_bytes() == b"a\r\nb\r\n"


# --------------------------------------------------------------------------- LED-001
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_write_and_edit_report_content_hashes(workspace) -> None:
    registry, _ = make_registry(workspace)
    target = workspace.root / "corpus" / "h.md"
    created = call(registry, "write_file", path="corpus/h.md", content="hello")
    assert created.data["sha256_before"] is None
    assert created.data["sha256_after"] == sha(target)
    before = sha(target)
    edited = edit(registry, "corpus/h.md", "hello", "bye")
    assert edited.data["sha256_before"] == before
    assert edited.data["sha256_after"] == sha(target)


def test_an_unchanged_write_reports_equal_hashes(workspace) -> None:
    registry, _ = make_registry(workspace)
    call(registry, "write_file", path="corpus/same.md", content="x")
    again = call(registry, "write_file", path="corpus/same.md", content="x")
    assert again.data["sha256_before"] == again.data["sha256_after"]


def test_the_ledger_carries_the_file_hashes(workspace) -> None:
    from test_w2a import make_agent, tool_call

    from panda_core.provider import DoneEvent, TextDelta

    agent, session, _, _ = make_agent(
        workspace,
        [
            [tool_call("write_file", path="corpus/l.md", content="hello")],
            [TextDelta("ok"), DoneEvent(finish_reason="stop")],
        ],
    )
    asyncio.run(agent.run_turn("write"))
    results = [e for e in session.events() if e.type == "tool.result"]
    assert results[0].data["sha256_after"] == sha(workspace.root / "corpus" / "l.md")
    ends = [e for e in session.events() if e.type == "turn.end"]
    assert HEX64.fullmatch(ends[-1].data["tree_sha256"])


def test_tree_hash_is_stable_across_copies_and_changes_with_one_byte(
    workspace, tmp_path
) -> None:
    import shutil

    from panda_core.treehash import tree_hash

    first = tree_hash(workspace.root)
    copy = tmp_path / "copy"
    shutil.copytree(workspace.root, copy, ignore=shutil.ignore_patterns(".panda"))
    assert tree_hash(copy).digest == first.digest
    (copy / "corpus" / "evidence.md").write_bytes(b"# evidence!\n")
    assert tree_hash(copy).digest != first.digest


def test_tree_hash_ignores_the_ledger_and_other_non_corpus_files(workspace) -> None:
    from panda_core.treehash import tree_hash

    before = tree_hash(workspace.root).digest
    SessionLog.create(workspace.sessions_dir, label="noise")
    (workspace.root / "scratch.tmp").write_text("x")
    assert tree_hash(workspace.root).digest == before


# --------------------------------------------------------------------------- SEC-006
def test_a_catastrophic_regex_is_stopped_and_the_loop_stays_responsive(
    workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("panda_core.tools.GREP_TIMEOUT_SECONDS", 1.0)
    (workspace.root / "corpus" / "evil.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
    registry, _ = make_registry(workspace)

    async def scenario() -> tuple[object, int, float]:
        ticks = 0
        stop = asyncio.Event()

        async def ticker() -> None:
            nonlocal ticks
            while not stop.is_set():
                await asyncio.sleep(0.05)
                ticks += 1

        watcher = asyncio.create_task(ticker())
        started = time.monotonic()
        result = await registry.call("grep", {"pattern": "(a+)+$", "glob": "corpus/evil.txt"})
        elapsed = time.monotonic() - started
        stop.set()
        await watcher
        return result, ticks, elapsed

    result, ticks, elapsed = asyncio.run(scenario())
    assert not result.ok and "longer than" in result.content
    assert elapsed < 6
    assert ticks >= 8, "the event loop was blocked while the regex ran"


@POSIX
def test_the_search_worker_does_not_outlive_a_timeout(
    workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setattr("panda_core.tools.GREP_TIMEOUT_SECONDS", 1.0)
    (workspace.root / "corpus" / "evil2.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
    pids: list[int] = []
    real = subprocess.Popen

    class Recording(real):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pids.append(self.pid)

    monkeypatch.setattr("panda_core.process.subprocess.Popen", Recording)
    registry, _ = make_registry(workspace)
    call(registry, "grep", pattern="(a+)+$", glob="corpus/evil2.txt")
    monkeypatch.undo()
    assert pids, "the search must run in a child process"
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_fixed_strings_cannot_be_made_catastrophic(workspace) -> None:
    (workspace.root / "corpus" / "f.txt").write_text("(a+)+$ " + "a" * 40 + "\n", encoding="utf-8")
    registry, _ = make_registry(workspace)
    result = call(registry, "grep", pattern="(a+)+$", glob="corpus/f.txt", fixed_strings=True)
    assert result.ok and result.data["count"] == 1


def test_neg_grep_results_keep_their_shape(workspace) -> None:
    (workspace.root / "corpus" / "g.txt").write_text("one\nTwo words\nthree\n", encoding="utf-8")
    registry, _ = make_registry(workspace)
    result = call(registry, "grep", pattern="two", glob="corpus/g.txt")
    assert result.ok and result.data["matches"] == ["corpus/g.txt:2: Two words"]
    assert result.data["files_scanned"] == 1 and result.data["truncated"] is False
    sensitive = call(registry, "grep", pattern="two", glob="corpus/g.txt", case_sensitive=True)
    assert sensitive.data["count"] == 0
    limited = call(registry, "grep", pattern="e", glob="corpus/g.txt", max_matches=1)
    assert limited.data["truncated"] is True and limited.data["count"] == 1


# ------------------------------------------------------------ providers (fixtures only)
import httpx  # noqa: E402

from panda_core.config import ProviderSettings  # noqa: E402
from panda_core.errors import ProtocolError  # noqa: E402
from panda_core.provider import DoneEvent as ProvDone  # noqa: E402
from panda_core.provider import OpenCodeProvider  # noqa: E402

OPENAI_MODEL = "deepseek-v4.1-flash"
RESPONSES_MODEL = "gpt-5.6"
GOOGLE_MODEL = "gemini-4"
ANTHROPIC_MODEL = "claude-sonnet-4-6"


def provider_for(body: str, *, model: str) -> OpenCodeProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenCodeProvider(
        ProviderSettings(max_retries=0), model=model, client=client, api_key="SYNTH_SECRET_KEY"
    )


async def drain(provider: OpenCodeProvider, model: str, messages=None):
    events = []
    async for event in provider.stream(
        messages or [{"role": "user", "content": "hi"}], None, model=model
    ):
        events.append(event)
    return events


def sse_of(*chunks: dict, done: bool = False, events: bool = False) -> str:
    out = ""
    for chunk in chunks:
        if events:
            out += f"event: {chunk['type']}\n"
        out += f"data: {json.dumps(chunk)}\n\n"
    return out + ("data: [DONE]\n\n" if done else "")


# --------------------------------------------------------------------------- PROV-002
TRUNCATED = [
    (OPENAI_MODEL, sse_of({"choices": [{"delta": {"content": "parcial"}}]})),
    (
        RESPONSES_MODEL,
        sse_of({"type": "response.output_text.delta", "delta": "parcial"}, events=True),
    ),
    (GOOGLE_MODEL, sse_of({"candidates": [{"content": {"parts": [{"text": "parcial"}]}}]})),
    (
        ANTHROPIC_MODEL,
        sse_of(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "p"},
            },
            events=True,
        ),
    ),
]


@pytest.mark.parametrize(("model", "body"), TRUNCATED)
def test_a_stream_cut_without_a_terminal_event_is_a_protocol_error(model, body) -> None:
    with pytest.raises(ProtocolError):
        asyncio.run(drain(provider_for(body, model=model), model))


COMPLETE = [
    (OPENAI_MODEL, sse_of({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})),
    (OPENAI_MODEL, sse_of({"choices": [{"delta": {"content": "ok"}}]}, done=True)),
    (
        RESPONSES_MODEL,
        sse_of(
            {"type": "response.output_text.delta", "delta": "ok"},
            {"type": "response.completed", "response": {}},
            events=True,
        ),
    ),
    (
        GOOGLE_MODEL,
        sse_of(
            {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
        ),
    ),
    (
        ANTHROPIC_MODEL,
        sse_of(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            },
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
            events=True,
        ),
    ),
]


@pytest.mark.parametrize(("model", "body"), COMPLETE)
def test_neg_a_complete_stream_still_finishes(model, body) -> None:
    events = asyncio.run(drain(provider_for(body, model=model), model))
    assert isinstance(events[-1], ProvDone)


# --------------------------------------------------------------------------- PROV-003
def test_an_sse_event_with_several_data_lines_is_joined_with_newlines() -> None:
    payload = json.dumps({"choices": [{"delta": {"content": "linha"}, "finish_reason": "stop"}]})
    half = payload.index('"delta"')
    body = f"data: {payload[:half]}\ndata: {payload[half:]}\n\n"
    events = asyncio.run(drain(provider_for(body, model=OPENAI_MODEL), OPENAI_MODEL))
    assert "".join(getattr(e, "text", "") for e in events) == "linha"


def test_sse_comments_and_unknown_fields_are_ignored() -> None:
    chunk = {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}
    body = f": keep-alive\nid: 7\nretry: 100\ndata: {json.dumps(chunk)}\n\n"
    events = asyncio.run(drain(provider_for(body, model=OPENAI_MODEL), OPENAI_MODEL))
    assert "".join(getattr(e, "text", "") for e in events) == "x"


# --------------------------------------------------------------------------- PROV-004
def tool_chunk(call_id: str | None = None) -> dict:
    fragment: dict = {"index": 0, "function": {"name": "read_file", "arguments": "{}"}}
    if call_id:
        fragment["id"] = call_id
    return {"choices": [{"delta": {"tool_calls": [fragment]}, "finish_reason": "tool_calls"}]}


def test_generated_tool_call_ids_do_not_repeat_between_steps() -> None:
    provider = provider_for(sse_of(tool_chunk()), model=OPENAI_MODEL)
    first = asyncio.run(drain(provider, OPENAI_MODEL))[-1].tool_calls[0].id
    second = asyncio.run(drain(provider, OPENAI_MODEL))[-1].tool_calls[0].id
    assert first != second


def test_neg_ids_supplied_by_the_provider_are_kept() -> None:
    provider = provider_for(sse_of(tool_chunk("toolu_ABC")), model=OPENAI_MODEL)
    assert asyncio.run(drain(provider, OPENAI_MODEL))[-1].tool_calls[0].id == "toolu_ABC"


# --------------------------------------------------------------------------- ADP-001 / ADP-002
from panda_core.provider import (  # noqa: E402
    convert_messages_for_anthropic,
    convert_messages_for_google,
    convert_messages_for_responses,
)


def assistant_call(call_id: str, name: str = "read_file", meta: dict | None = None) -> dict:
    call = {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
    if meta:
        call["provider_meta"] = meta
    return call


def tool_result(call_id: str, text: str = "result") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def test_anthropic_gets_a_tool_result_for_every_tool_use() -> None:
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [assistant_call("A")]},
        {"role": "user", "content": "next"},
    ]
    _, turns = convert_messages_for_anthropic(history)
    user_turn = turns[2]
    assert user_turn["role"] == "user"
    first = user_turn["content"][0]
    assert first["type"] == "tool_result" and first["tool_use_id"] == "A" and first["is_error"]
    assert user_turn["content"][1]["type"] == "text"


def test_anthropic_only_the_missing_result_is_synthesized() -> None:
    history = [
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [assistant_call("A"), assistant_call("B")],
        },
        tool_result("A", "real"),
    ]
    _, turns = convert_messages_for_anthropic(history)
    results = {b["tool_use_id"]: b for b in turns[-1]["content"] if b["type"] == "tool_result"}
    assert set(results) == {"A", "B"}
    assert results["A"]["content"] == "real" and results["B"].get("is_error")


def test_anthropic_a_dangling_last_tool_use_gets_a_closing_user_turn() -> None:
    history = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [assistant_call("A")]},
    ]
    _, turns = convert_messages_for_anthropic(history)
    assert turns[-1]["role"] == "user" and turns[-1]["content"][0]["tool_use_id"] == "A"


def test_neg_anthropic_complete_pairs_are_unchanged() -> None:
    history = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [assistant_call("A")]},
        tool_result("A", "ok"),
        {"role": "assistant", "content": "done"},
    ]
    _, turns = convert_messages_for_anthropic(history)
    blocks = [b for t in turns for b in t["content"] if b["type"] == "tool_result"]
    assert len(blocks) == 1 and "is_error" not in blocks[0]


def test_responses_gets_an_output_for_every_function_call_and_drops_orphans() -> None:
    history = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [assistant_call("A")]},
        tool_result("ZZZ", "orphan"),
        {"role": "user", "content": "next"},
    ]
    _, items = convert_messages_for_responses(history)
    outputs = [i for i in items if i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["A"]
    call_at = next(n for n, i in enumerate(items) if i.get("type") == "function_call")
    assert items[call_at + 1]["type"] == "function_call_output"


def test_neg_responses_complete_pairs_are_unchanged() -> None:
    history = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [assistant_call("A")]},
        tool_result("A", "ok"),
    ]
    _, items = convert_messages_for_responses(history)
    assert [i.get("output") for i in items if i.get("type") == "function_call_output"] == ["ok"]


def test_google_history_never_starts_with_the_model() -> None:
    history = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": "I was the first message"},
        {"role": "user", "content": "task"},
    ]
    _, contents = convert_messages_for_google(history)
    assert contents[0]["role"] == "user"


def test_google_orphan_function_responses_are_dropped_with_their_calls() -> None:
    history = [
        {"role": "assistant", "content": None, "tool_calls": [assistant_call("A")]},
        tool_result("A", "orphaned by the trim"),
        {"role": "user", "content": "task"},
    ]
    _, contents = convert_messages_for_google(history)
    assert contents[0]["role"] == "user"
    assert not any("functionResponse" in part for c in contents for part in c["parts"])


def test_neg_google_thought_signature_still_round_trips() -> None:
    meta = {"google": {"thought_signature": "SIG123"}}
    history = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [assistant_call("A", meta=meta)]},
        tool_result("A", "ok"),
    ]
    _, contents = convert_messages_for_google(history)
    parts = [p for c in contents for p in c["parts"]]
    assert any(p.get("thoughtSignature") == "SIG123" for p in parts)
    assert any("functionResponse" in p for p in parts)
