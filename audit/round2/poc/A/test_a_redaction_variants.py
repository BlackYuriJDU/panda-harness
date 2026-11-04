"""Trilha A — variantes de SEC-004 (formatos que o redator não cobre)."""

from __future__ import annotations

import json

import pytest

from panda_core.redact import redact, redact_text

S = "SYNTH_SECRET_9f3c1e7a"


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"password": S}),                       # JSON em texto (conteúdo de tool.result)
        json.dumps({"api_key": S}, indent=2),
        f"postgres://admin:{S}@db.internal:5432/app",     # credencial em URL
        f"Cookie: session={S}",
        f"pwd={S}",
        f"passphrase: {S}",
        f"DATABASE_URL=postgres://u:{S}@h/db",
        "sk_live_" + "51H8" + "a" * 24,                    # Stripe (underscore)
        "glpat-" + "A" * 20,                               # GitLab PAT
        "hf_" + "A" * 34,                                  # Hugging Face
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIF" + "A" * 40 + "\n-----END ENCRYPTED PRIVATE KEY-----",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQ" + "A" * 40 + "\n-----END PGP PRIVATE KEY BLOCK-----",
        f"TOKEN = '{S}&x'",                                # valor curto antes de '&' (controle)
    ],
)
def test_redact_text_removes_the_secret(text):
    out = redact_text(text)
    secret_bits = [S, "sk_live_51H8", "glpat-AAAA", "hf_AAAA", "MIIFAAAA", "lQAAAA"]
    leaked = [bit for bit in secret_bits if bit in out]
    assert not leaked, f"vazou {leaked} em {out!r}"


def test_redact_dict_with_json_string_value():
    # tool.result costuma guardar o conteúdo como string JSON
    payload = {"content": json.dumps({"ok": True, "text": json.dumps({"client_secret": S})})}
    assert S not in json.dumps(redact(payload))


def test_ledger_does_not_keep_a_json_secret_read_by_the_agent(workspace):
    """E2E: read_file de um JSON com "password" -> tool.result no ledger."""

    import asyncio

    from panda_core.agent import Agent
    from panda_core.policy import PolicyEngine, load_policy_or_default
    from panda_core.provider import DoneEvent, ToolCall
    from panda_core.session import SessionLog
    from panda_core.tools import ToolContext, ToolRegistry

    (workspace.root / "corpus" / "settings.json").write_text(
        json.dumps({"db": {"password": S}}), encoding="utf-8"
    )

    class Script:
        def __init__(self):
            self.n = 0

        async def stream(self, messages, tools=None, model=None, session_id=None):
            self.n += 1
            if self.n == 1:
                call = ToolCall(id="c1", name="read_file",
                                arguments=json.dumps({"path": "corpus/settings.json"}))
                yield DoneEvent(finish_reason="tool_calls", tool_calls=(call,))
            else:
                yield DoneEvent(finish_reason="stop")

    session = SessionLog.create(workspace.sessions_dir, label="poc")
    policy, _ = load_policy_or_default(workspace.policy_path)
    registry = ToolRegistry(ToolContext(workspace=workspace,
                                        policy=PolicyEngine(policy, root=workspace.root),
                                        session=session))
    agent = Agent(workspace, Script(), registry, session=session, max_steps=3)
    asyncio.run(agent.run_turn("leia"))
    assert S not in session.path.read_text(encoding="utf-8"), "segredo JSON gravado no ledger"
