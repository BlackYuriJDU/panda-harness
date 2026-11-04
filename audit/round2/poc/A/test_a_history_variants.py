"""Trilha A — variantes de ADP-001 / PROV-004 / SEC-010."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from panda_core.config import ProviderSettings
from panda_core.provider import OpenCodeProvider


def capture(model):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(400, json={"error": {"message": "stop"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenCodeProvider(ProviderSettings(max_retries=0), model=model, client=client,
                                api_key="SYNTH_SECRET_KEY")
    return provider, seen


async def send(provider, model, messages):
    try:
        async for _ in provider.stream(messages, None, model=model):
            pass
    except Exception:  # noqa: BLE001 - a resposta 400 é proposital
        pass


def call(cid, name="read_file"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}


# ADP-001 (variante): histórico OpenAI chat com tool_call sem resposta.
def test_openai_chat_gets_a_tool_message_for_every_tool_call():
    provider, seen = capture("deepseek-v4.1-flash")
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [call("A"), call("B")]},
        {"role": "tool", "tool_call_id": "A", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    asyncio.run(send(provider, "deepseek-v4.1-flash", messages))
    answered = {m.get("tool_call_id") for m in seen["body"]["messages"] if m.get("role") == "tool"}
    assert {"A", "B"} <= answered, f"tool_call B sem resposta no corpo: {answered}"


# R2-123 (PROV-004 por outra rota): ids repetidos fornecidos pelo provedor entre passos
# chegam duplicados ao protocolo Anthropic (troca de modelo no meio da sessão).
def test_anthropic_body_has_unique_tool_use_ids_even_if_the_history_repeats_them():
    provider, seen = capture("claude-sonnet-4-6")
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [call("call_0")]},
        {"role": "tool", "tool_call_id": "call_0", "content": "first"},
        {"role": "assistant", "content": None, "tool_calls": [call("call_0")]},
        {"role": "tool", "tool_call_id": "call_0", "content": "second"},
    ]
    asyncio.run(send(provider, "claude-sonnet-4-6", messages))
    ids = [b["id"] for m in seen["body"]["messages"] if isinstance(m.get("content"), list)
           for b in m["content"] if b.get("type") == "tool_use"]
    assert len(ids) == len(set(ids)), f"tool_use ids duplicados: {ids}"


# SEC-010 (refutação esperada): links codificados ou com barra invertida.
@pytest.mark.parametrize("link", ["..\\\\..\\\\README.md", "%2e%2e/%2e%2e/README.md", "./../x.md"])
def test_index_links_encoded_or_backslashed_stay_inside(workspace, link):
    from panda_core.errors import ConfigError
    from panda_core.registry import find_study, load_study_card

    index = workspace.root / "corpus" / "studies" / "INDEX.md"
    index.write_text(index.read_text(encoding="utf-8").replace("(gse160208.md)", f"({link})"),
                     encoding="utf-8")
    try:
        study = find_study(workspace.root, "gse160208")
        card = load_study_card(study, workspace.root)
    except ConfigError:
        return
    assert "fixture corpus" not in card["body"]
