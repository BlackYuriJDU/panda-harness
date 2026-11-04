"""Trilha A — variantes de PROV-002 / PROV-004."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from panda_core.config import ProviderSettings
from panda_core.errors import ProtocolError
from panda_core.provider import DoneEvent, OpenCodeProvider

ANTHROPIC_MODEL = "claude-sonnet-4-6"
RESPONSES_MODEL = "gpt-5.6"
GOOGLE_MODEL = "gemini-4"


def provider_for(body: str, *, model: str) -> OpenCodeProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenCodeProvider(ProviderSettings(max_retries=0), model=model, client=client,
                            api_key="SYNTH_SECRET_KEY")


async def drain(provider, model):
    return [e async for e in provider.stream([{"role": "user", "content": "hi"}], None, model=model)]


def sse(*chunks, done=False):
    out = "".join(f"event: {c['type']}\ndata: {json.dumps(c)}\n\n" if "type" in c
                  else f"data: {json.dumps(c)}\n\n" for c in chunks)
    return out + ("data: [DONE]\n\n" if done else "")


# R2-120: protocolos que não usam '[DONE]' (Anthropic, Responses, Google) aceitam um
# '[DONE]' como evento terminal; um fluxo cortado no meio de uma tool call vira completo.
CUT = {
    ANTHROPIC_MODEL: sse(
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "write_file", "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"corpus/a.md\""}},
        done=True),
    RESPONSES_MODEL: sse({"type": "response.output_text.delta", "delta": "parcial"}, done=True),
    GOOGLE_MODEL: sse({"candidates": [{"content": {"parts": [{"text": "parcial"}]}}]}, done=True),
}


@pytest.mark.parametrize("model", list(CUT))
def test_done_marker_is_not_terminal_for_protocols_that_do_not_use_it(model):
    with pytest.raises(ProtocolError):
        events = asyncio.run(drain(provider_for(CUT[model], model=model), model))
        done = [e for e in events if isinstance(e, DoneEvent)]
        raise AssertionError(f"{model}: '[DONE]' aceito como fim: {done}")
