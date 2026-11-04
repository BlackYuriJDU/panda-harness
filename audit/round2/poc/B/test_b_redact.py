"""Trilha B — redact.py e onde a redação (não) é aplicada. Afirma o resultado SEGURO."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from panda_core.agent import Agent
from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.provider import DoneEvent, ToolCall
from panda_core.redact import redact_text
from panda_core.session import SessionLog
from panda_core.tools import ToolContext, ToolRegistry

S = "SYNTH_SECRET_abcdef123456"


@pytest.mark.parametrize(
    "text",
    [
        f'{{"api_key": "{S}"}}',                                  # JSON (chave entre aspas)
        f'{{"password":"{S}"}}',
        f"postgres://admin:{S}@db.example.org:5432/x",             # userinfo em URL
        f"Authorization: Basic {S}",                                # cabeçalho Basic
        f"Cookie: sessionid={S}",                                   # cabeçalho Cookie
        f"Set-Cookie: auth={S}; HttpOnly",
        f'password = "correct horse {S}"',                          # valor com espaço
        f"-----BEGIN ENCRYPTED PRIVATE KEY-----\n{S}\n-----END ENCRYPTED PRIVATE KEY-----",
        f"-----BEGIN DSA PRIVATE KEY-----\n{S}\n-----END DSA PRIVATE KEY-----",
        f"-----BEGIN PGP PRIVATE KEY BLOCK-----\n{S}\n-----END PGP PRIVATE KEY BLOCK-----",
        f"sk_live_{S}",                                             # Stripe
        f"glpat-{S}",                                               # GitLab
        f"hf_{S}xxxxxxxx",                                          # Hugging Face
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJTWU5USF9TRUNSRVQifQ.SYNTH_SECRET_sig",  # JWT
        f"pwd={S}",
    ],
)
def test_redaction_covers_common_secret_formats(text: str) -> None:
    out = redact_text(text)
    assert "SYNTH_SECRET" not in out, out


def test_redaction_keeps_biomedical_prose() -> None:
    text = "Secretion: increased_in_GSE160208; Tokenization: wordpiece_v2"
    assert redact_text(text) == text


def test_redaction_is_linear_on_hostile_input() -> None:
    text = ("secret" * 200_000) + ":"
    started = time.perf_counter()
    redact_text(text)
    assert time.perf_counter() - started < 2.0


class _Provider:
    def __init__(self, script: list[list[Any]]) -> None:
        self.script, self.calls = list(script), []

    async def stream(self, messages, tools=None, model=None, session_id=None):  # type: ignore[no-untyped-def]
        self.calls.append(json.dumps(messages))
        for event in self.script.pop(0) if self.script else [DoneEvent(finish_reason="stop")]:
            yield event


def test_tool_results_are_redacted_before_they_reach_the_provider(workspace) -> None:  # type: ignore[no-untyped-def]
    (workspace.root / "corpus" / "lab.md").write_text(
        f"api_key = {S}\nghp_{'A' * 30}\n", encoding="utf-8"
    )
    session = SessionLog.create(workspace.sessions_dir, label="b")
    policy, _ = load_policy_or_default(workspace.policy_path)
    registry = ToolRegistry(ToolContext(workspace=workspace, policy=PolicyEngine(policy),
                                        session=session))
    call = ToolCall(id="c1", name="read_file", arguments=json.dumps({"path": "corpus/lab.md"}))
    provider = _Provider([[DoneEvent(finish_reason="tool_calls", tool_calls=[call])],
                          [DoneEvent(finish_reason="stop")]])
    agent = Agent(workspace, provider, registry, session=session, max_steps=3)  # type: ignore[arg-type]
    asyncio.run(agent.run_turn("leia corpus/lab.md"))
    ledger = workspace.sessions_dir.joinpath(f"{session.session_id}.jsonl").read_text()
    assert S not in ledger  # o ledger é redigido
    assert S not in provider.calls[-1], "segredo do resultado da ferramenta foi ao provedor"


def test_hostile_instructions_file_does_not_stall_prompt_build(workspace) -> None:  # type: ignore[no-untyped-def]
    from panda_core.agent import build_system_prompt

    workspace.instructions_path.write_text("secret" * 330_000 + ":", encoding="utf-8")
    started = time.perf_counter()
    build_system_prompt(workspace)
    assert time.perf_counter() - started < 2.0, time.perf_counter() - started


def test_json_secrets_in_tool_results_are_redacted_in_the_ledger(workspace) -> None:  # type: ignore[no-untyped-def]
    (workspace.root / "corpus" / "svc.json").write_text(
        json.dumps({"client_secret": S, "refresh_token": S}), encoding="utf-8"
    )
    session = SessionLog.create(workspace.sessions_dir, label="b")
    policy, _ = load_policy_or_default(workspace.policy_path)
    registry = ToolRegistry(ToolContext(workspace=workspace, policy=PolicyEngine(policy),
                                        session=session))
    call = ToolCall(id="c1", name="read_file", arguments=json.dumps({"path": "corpus/svc.json"}))
    provider = _Provider([[DoneEvent(finish_reason="tool_calls", tool_calls=[call])],
                          [DoneEvent(finish_reason="stop")]])
    agent = Agent(workspace, provider, registry, session=session, max_steps=3)  # type: ignore[arg-type]
    asyncio.run(agent.run_turn("leia"))
    ledger = workspace.sessions_dir.joinpath(f"{session.session_id}.jsonl").read_text()
    assert S not in ledger
