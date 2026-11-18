"""Remote tools and MCP catalog against a local stand-in for the task container."""

from __future__ import annotations

import asyncio
import json

import pytest

from panda_ple import mcp_tools
from panda_ple.mcp_tools import McpTools
from panda_ple.remote_tools import RemoteToolset, head_tail
from tests.fakes import FakeSession, McpApp

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def toolset(tmp_path, **kwargs):
    session = FakeSession(tmp_path / "app", time_left=kwargs.pop("time_left", None))
    session.container.root.mkdir(parents=True, exist_ok=True)
    tools = RemoteToolset(session, backup_root=str(tmp_path / "backup"), **kwargs)
    return session, tools


def call(tools, name, **args):
    return asyncio.run(tools.call(name, args))


def test_bash_runs_in_the_workdir_and_reports_exit_codes(tmp_path) -> None:
    session, tools = toolset(tmp_path)
    ok = call(tools, "bash", command="pwd && echo hi")
    assert ok.ok and ok.content.startswith("exit_code: 0")
    assert str(session.container.root) in ok.content and "hi" in ok.content
    bad = call(tools, "bash", command="exit 3")
    assert not bad.ok and bad.content.startswith("exit_code: 3")


def test_bash_timeout_respects_the_remaining_time(tmp_path) -> None:
    session, tools = toolset(tmp_path, time_left=40)
    call(tools, "bash", command="true", timeout_sec=600)
    assert session.container.requests[-1]["timeout_sec"] == 10


def test_write_read_and_edit_round_trip(tmp_path) -> None:
    session, tools = toolset(tmp_path)
    assert call(tools, "write_file", path="out/report.md", content="alpha\nbeta\n").ok
    assert (session.container.root / "out/report.md").read_text() == "alpha\nbeta\n"
    text = call(tools, "read_file", path="out/report.md").content
    assert "     1  alpha" in text and "of 2 lines" in text
    assert call(tools, "edit_file", path="out/report.md", old_text="beta", new_text="gamma").ok
    assert (session.container.root / "out/report.md").read_text() == "alpha\ngamma\n"
    assert tools.written == [str(session.container.root / "out/report.md")]


def test_edit_refuses_ambiguous_or_missing_text(tmp_path) -> None:
    session, tools = toolset(tmp_path)
    (session.container.root / "a.txt").write_text("x x")
    twice = call(tools, "edit_file", path="a.txt", old_text="x", new_text="y")
    assert not twice.ok and json.loads(twice.content)["next_step"]
    missing = call(tools, "edit_file", path="a.txt", old_text="z", new_text="y")
    assert not missing.ok
    assert call(tools, "edit_file", path="a.txt", old_text="x", new_text="y", replace_all=True).ok
    assert (session.container.root / "a.txt").read_text() == "y y"


def test_original_files_are_backed_up_before_overwrite(tmp_path) -> None:
    session, tools = toolset(tmp_path)
    tools.scratch_prefix = "/scratch-only/"  # pytest's tmp_path itself lives under /tmp
    original = session.container.root / "inputs/spec.csv"
    original.parent.mkdir()
    original.write_text("original")
    call(tools, "write_file", path=str(original), content="changed")
    call(tools, "write_file", path=str(original), content="changed again")
    backup = tmp_path / "backup" / str(original).lstrip("/")
    assert backup.read_text() == "original"
    assert original.read_text() == "changed again"


def test_binary_files_point_to_the_right_tool(tmp_path) -> None:
    session, tools = toolset(tmp_path)
    (session.container.root / "plan.pdf").write_bytes(b"%PDF\x00\x01")
    result = call(tools, "read_file", path="plan.pdf")
    body = json.loads(result.content)
    assert not result.ok and "binary" in body["error"] and "pdfplumber" in body["next_step"]


def test_errors_follow_the_contract(tmp_path) -> None:
    _, tools = toolset(tmp_path)
    for result in (
        call(tools, "read_file", path="missing.txt"),
        call(tools, "nope"),
        asyncio.run(tools.call("bash", "{not json")),
    ):
        body = json.loads(result.content)
        assert not result.ok
        assert set(body) == {"ok", "error", "cause", "next_step"}


def test_view_image_only_with_vision(tmp_path) -> None:
    session, tools = toolset(tmp_path)
    names = [spec["function"]["name"] for spec in tools.to_openai()]
    assert "view_image" not in names
    session, tools = toolset(tmp_path, vision=True)
    (session.container.root / "sheet.png").write_bytes(PNG)
    result = call(tools, "view_image", path="sheet.png")
    assert result.ok and result.images[0][0] == "image/png"
    (session.container.root / "fake.png").write_text("text")
    assert not call(tools, "view_image", path="fake.png").ok


def test_head_tail_keeps_both_ends() -> None:
    text = "START" + "x" * 50_000 + "END"
    cut = head_tail(text, 1_000)
    assert cut.startswith("START") and cut.endswith("END") and "omitted" in cut


def test_mcp_catalog_is_direct_when_small(tmp_path) -> None:
    app = McpApp()
    session = FakeSession(tmp_path, mcp=app)
    catalog = asyncio.run(McpTools.discover(session))
    tools = RemoteToolset(session, mcp=catalog, backup_root=str(tmp_path / "b"))
    names = [spec["function"]["name"] for spec in tools.to_openai()]
    assert "mcp__apps__secret_word" in names
    result = call(tools, "mcp__apps__secret_word")
    assert result.ok and result.content == app.word
    assert "1 tools" in catalog.summary()


def test_mcp_catalog_uses_find_and_call_when_large(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_tools, "DIRECT_MAX_TOOLS", 3)
    app = McpApp(tools=10)
    session = FakeSession(tmp_path, mcp=app)
    catalog = asyncio.run(McpTools.discover(session))
    tools = RemoteToolset(session, mcp=catalog, backup_root=str(tmp_path / "b"))
    names = [spec["function"]["name"] for spec in tools.to_openai()]
    assert "mcp_find" in names and "mcp_call" in names
    assert not any(name.startswith("mcp__") for name in names)
    found = json.loads(call(tools, "mcp_find", query="secret word").content)
    assert found["matches"][0]["tool"] == "secret_word"
    result = call(tools, "mcp_call", server="apps", tool="secret_word", arguments={})
    assert result.content == app.word
    assert not call(tools, "mcp_call", server="apps", tool="missing").ok


def test_unreachable_mcp_server_is_reported_not_fatal(tmp_path) -> None:
    session = FakeSession(tmp_path, mcp=McpApp())
    session.mcp_servers = [{"name": "broken", "url": "http://127.0.0.1:9/mcp/"}]
    catalog = asyncio.run(McpTools.discover(session))
    assert catalog.tools == [] and "broken" in catalog.summary()


@pytest.mark.parametrize("value", [None, 5, 900])
def test_timeout_is_clamped(tmp_path, value) -> None:
    _, tools = toolset(tmp_path)
    assert 1 <= tools._timeout(value) <= 600


def test_scratch_files_are_not_backed_up(tmp_path) -> None:
    session, tools = toolset(tmp_path)
    tools.scratch_prefix = str(session.container.root) + "/"
    scratch = session.container.root / "draft.py"
    scratch.write_text("v1")
    call(tools, "write_file", path=str(scratch), content="v2")
    assert not (tmp_path / "backup").exists()
