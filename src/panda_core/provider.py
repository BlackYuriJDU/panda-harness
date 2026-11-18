"""OpenCode Go provider (https://opencode.ai/inference/go).

Go exposes four wire protocols under one key: OpenAI-compatible chat
completions at `/openai/v1/chat/completions`, Anthropic Messages at
`/anthropic/v1/messages`, the OpenAI Responses API at `/openai/v1/responses`,
and Google generateContent at `/google/v1beta/models/<id>:streamGenerateContent`.
All four adapters normalize their events into the same `ProviderEvent` stream,
so the agent loop never knows which wire protocol the model speaks.

Gemini requires the `thoughtSignature` returned with a function call to be
echoed back on the next turn; it travels in `ToolCall.metadata` and is stored
on the assistant message as `provider_meta`, never sent to other protocols.

Go also requires two non-generic headers on every request: a stable
`x-opencode-session` id per conversation and a `panda-harness/<version>`
User-Agent. Without them the endpoint answers 400 `MissingSessionID`.

The `generic` endpoint profile targets any gateway serving the same protocols
under caller-supplied base URLs (the PLE model gateway, for example): routes are
`{base}/chat/completions`, `{anthropic_base}/v1/messages`, and
`{google_base}/v1beta/models/...`, and no Go session header is sent.

Claude thinking blocks are signed and bound to the conversation prefix that
produced them. The Anthropic adapter returns the turn's ordered content blocks
in `DoneEvent.metadata["anthropic_content"]`; the agent stores them as the
assistant message's `provider_meta` and the converter replays them verbatim.
Any non-append edit of the history must drop them first (see agent `_trim`).

No secret ever appears in an error message. The API key comes from the
environment or the user-scoped credential store (never the workspace) and is
only used in the Authorization header.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from panda_core.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    ENDPOINT_GENERIC,
    PandaConfig,
    ProviderSettings,
    accepted_providers,
    split_model,
)
from panda_core.credentials import get_api_key
from panda_core.errors import ConfigError, ProviderError
from panda_core.protocols import (
    PROTOCOL_ANTHROPIC,
    PROTOCOL_GOOGLE,
    PROTOCOL_OPENAI_CHAT,
    PROTOCOL_RESPONSES,
    SUPPORTED_PROTOCOLS,
    classify_protocol,
    is_free_model,
    is_supported_model,
    model_protocol,
)
from panda_core.redact import redact_text
from panda_core.version import __version__

ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = f"panda-harness/{__version__}"
ANTHROPIC_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "pause_turn": "stop",
    "refusal": "content_filter",
}
GOOGLE_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "OTHER": "stop",
}


@dataclass(frozen=True)
class ModelInfo:
    id: str
    protocol: str
    supported: bool
    free: bool
    owned_by: str = "opencode"
    created: int | None = None

    @classmethod
    def from_id(cls, model_id: str, *, owned_by: str = "opencode", created: int | None = None):
        protocol = classify_protocol(model_id)
        return cls(
            id=model_id,
            protocol=protocol,
            supported=protocol in SUPPORTED_PROTOCOLS,
            free=is_free_model(model_id),
            owned_by=owned_by,
            created=created,
        )

    def qualified(self, provider: str = DEFAULT_PROVIDER) -> str:
        return f"{provider}/{self.id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "protocol": self.protocol,
            "supported": self.supported,
            "free": self.free,
            "owned_by": self.owned_by,
            "created": self.created,
        }


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None

    @property
    def context_tokens(self) -> int:
        return self.prompt_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
        }


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _cost_value(raw: dict[str, Any]) -> float | None:
    """Read the cost Go may report, preferring `usage.opencode.list_cost`."""

    candidates: list[Any] = []
    extra = raw.get("opencode")
    if isinstance(extra, dict):
        candidates.append(extra.get("list_cost"))
    candidates.extend([raw.get("cost"), raw.get("total_cost"), raw.get("cost_usd")])
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return float(candidate)
        except (TypeError, ValueError):
            continue
    return None


def parse_usage(raw: Any) -> Usage | None:
    """Normalize OpenAI usage, including cached prompt tokens and cost if sent."""

    if not isinstance(raw, dict) or not raw:
        return None
    details = raw.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, dict):
        cached = _as_int(details.get("cached_tokens"))
    if not cached:
        cached = _as_int(raw.get("cache_read_input_tokens"))
    prompt = _as_int(raw.get("prompt_tokens"))
    completion = _as_int(raw.get("completion_tokens"))
    total = _as_int(raw.get("total_tokens")) or (prompt + completion)
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=cached,
        cache_write_tokens=_as_int(raw.get("cache_creation_input_tokens")),
        cost_usd=_cost_value(raw),
    )


def parse_anthropic_usage(raw: Any) -> Usage | None:
    """Normalize Anthropic Messages usage into the shared `Usage` shape."""

    if not isinstance(raw, dict) or not raw:
        return None
    prompt = _as_int(raw.get("input_tokens"))
    completion = _as_int(raw.get("output_tokens"))
    total = _as_int(raw.get("total_tokens")) or (prompt + completion)
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=_as_int(raw.get("cache_read_input_tokens")),
        cache_write_tokens=_as_int(raw.get("cache_creation_input_tokens")),
        cost_usd=_cost_value(raw),
    )


def merge_usage(target: dict[str, Any], raw: Any) -> None:
    """Merge partial Anthropic usage frames (start carries input, delta output)."""

    if not isinstance(raw, dict):
        return
    for key, value in raw.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = value
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_usage(target[key], value)
        elif isinstance(value, dict):
            target[key] = dict(value)


def parse_responses_usage(raw: Any) -> Usage | None:
    """Normalize Responses API usage (`input_tokens`/`output_tokens`)."""

    if not isinstance(raw, dict) or not raw:
        return None
    prompt = _as_int(raw.get("input_tokens"))
    completion = _as_int(raw.get("output_tokens"))
    total = _as_int(raw.get("total_tokens")) or (prompt + completion)
    details = raw.get("input_tokens_details")
    cached = 0
    cache_write = 0
    if isinstance(details, dict):
        cached = _as_int(details.get("cached_tokens"))
        cache_write = _as_int(details.get("cache_write_tokens"))
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=cached,
        cache_write_tokens=cache_write,
        cost_usd=_cost_value(raw),
    )


def parse_google_usage(raw: Any) -> Usage | None:
    """Normalize Gemini `usageMetadata` into the shared `Usage` shape."""

    if not isinstance(raw, dict) or not raw:
        return None
    prompt = _as_int(raw.get("promptTokenCount"))
    completion = _as_int(raw.get("candidatesTokenCount"))
    total = _as_int(raw.get("totalTokenCount")) or (prompt + completion)
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=_as_int(raw.get("cachedContentTokenCount")),
        cost_usd=_cost_value(raw),
    )


_OPENAI_MESSAGE_KEYS = frozenset({"role", "content", "name", "tool_call_id"})


def sanitize_messages_for_openai(
    messages: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop harness-only keys so OpenAI-compatible endpoints accept the history.

    `provider_meta` carries Gemini thought signatures; it must never reach
    another protocol. Assistant tool calls are rebuilt from their public shape.
    """

    cleaned: list[dict[str, Any]] = []
    pending_images: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if pending_images and message.get("role") != "tool":
            cleaned.append(_tool_images_message(pending_images))
            pending_images = []
        item = {key: message[key] for key in _OPENAI_MESSAGE_KEYS if key in message}
        if message.get("role") == "tool" and isinstance(item.get("content"), list):
            pending_images.extend(_image_parts(item["content"]))
            item["content"] = _text_content(item["content"])
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            calls: list[dict[str, Any]] = []
            for call in raw_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                calls.append(
                    {
                        "id": str(call.get("id") or ""),
                        "type": "function",
                        "function": (
                            {
                                "name": str(function.get("name") or ""),
                                "arguments": str(function.get("arguments") or "{}"),
                            }
                            if isinstance(function, dict)
                            else {"name": "", "arguments": "{}"}
                        ),
                    }
                )
            if calls:
                item["tool_calls"] = calls
        cleaned.append(item)
    if pending_images:
        cleaned.append(_tool_images_message(pending_images))
    return cleaned


def _image_parts(value: Any) -> list[tuple[str, str]]:
    """(media_type, base64) pairs from OpenAI-style `image_url` data-URL parts."""

    images: list[tuple[str, str]] = []
    if not isinstance(value, list):
        return images
    for part in value:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else image
        if not isinstance(url, str) or not url.startswith("data:"):
            continue
        header, _, data = url[5:].partition(",")
        media_type, _, encoding = header.partition(";")
        if encoding == "base64" and media_type.startswith("image/") and data:
            images.append((media_type, data))
    return images


def _anthropic_images(images: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {"type": "image", "source": {"type": "base64", "media_type": kind, "data": data}}
        for kind, data in images
    ]


def _google_images(images: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [{"inlineData": {"mimeType": kind, "data": data}} for kind, data in images]


def _tool_images_message(images: list[tuple[str, str]]) -> dict[str, Any]:
    """OpenAI chat tool messages are text-only; images follow as a user turn."""

    parts: list[dict[str, Any]] = [
        {"type": "text", "text": "Images returned by the tool calls above."}
    ]
    for kind, data in images:
        parts.append({"type": "image_url", "image_url": {"url": f"data:{kind};base64,{data}"}})
    return {"role": "user", "content": parts}


def _text_content(value: Any) -> str:
    """Flatten string or text-block message content into plain text."""

    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for block in value:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _tool_input(arguments: Any) -> dict[str, Any]:
    """Convert OpenAI tool arguments (JSON text) into an Anthropic input object."""

    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError:
        # The tool result already reports the parse failure to the model.
        return {}
    return value if isinstance(value, dict) else {}


def _append_blocks(
    converted: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
) -> None:
    if not blocks:
        return
    if converted and converted[-1]["role"] == role:
        converted[-1]["content"].extend(blocks)
    else:
        converted.append({"role": role, "content": list(blocks)})


def _normalize_anthropic_turns(
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Make history acceptable to the Anthropic Messages API.

    Anthropic requires the first turn to be `user` and rejects `tool_result`
    blocks whose `tool_use_id` was not emitted by the immediately preceding
    assistant turn. Context trimming can produce both shapes, so drop the
    invalid leading assistant turns and the orphan tool results.
    """

    def filter_orphans(
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        pending: set[str] = set()
        for turn in items:
            role = turn["role"]
            blocks = turn["content"]
            if role == "assistant":
                pending = {
                    str(block.get("id"))
                    for block in blocks
                    if block.get("type") == "tool_use" and block.get("id")
                }
                filtered.append(turn)
                continue
            kept = [
                block
                for block in blocks
                if block.get("type") != "tool_result"
                or str(block.get("tool_use_id") or "") in pending
            ]
            pending = set()
            if kept:
                filtered.append({"role": role, "content": kept})
        return filtered

    normalized = filter_orphans(turns)
    while normalized and normalized[0]["role"] == "assistant":
        normalized.pop(0)
    # Dropping an assistant can orphan the tool results of the next user turn.
    normalized = filter_orphans(normalized)
    merged: list[dict[str, Any]] = []
    for turn in normalized:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"].extend(turn["content"])
        else:
            merged.append({"role": turn["role"], "content": list(turn["content"])})
    return merged


def _anthropic_content(message: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return a deep copy of the stored Anthropic blocks of an assistant turn."""

    meta = message.get("provider_meta")
    blocks = meta.get("anthropic_content") if isinstance(meta, dict) else None
    if not isinstance(blocks, list) or not blocks:
        return None
    if not all(isinstance(block, dict) and block.get("type") for block in blocks):
        return None
    return copy.deepcopy(blocks)


def is_claude_model(model_id: str) -> bool:
    """Claude rejects sampling parameters; detect it on any protocol."""

    return model_id.strip().lower().startswith("claude")


EPHEMERAL_CACHE = {"type": "ephemeral"}


def _mark_last_block_for_cache(messages: list[dict[str, Any]]) -> None:
    """Put a cache breakpoint on the newest block that accepts one."""

    if not messages:
        return
    content = messages[-1].get("content")
    if not isinstance(content, list):
        return
    for block in reversed(content):
        if isinstance(block, dict) and block.get("type") not in {
            "thinking",
            "redacted_thinking",
        }:
            block["cache_control"] = dict(EPHEMERAL_CACHE)
            return


def _cache_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the system prompt and the newest text message for Claude caching."""

    def as_parts(message: dict[str, Any]) -> dict[str, Any]:
        text = message.get("content")
        if not isinstance(text, str) or not text:
            return message
        return {
            **message,
            "content": [
                {"type": "text", "text": text, "cache_control": dict(EPHEMERAL_CACHE)}
            ],
        }

    marked = list(messages)
    if marked and marked[0].get("role") == "system":
        marked[0] = as_parts(marked[0])
    if len(marked) > 1 and marked[-1].get("role") in {"user", "tool"}:
        marked[-1] = as_parts(marked[-1])
    return marked


def convert_messages_for_anthropic(
    messages: Sequence[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Translate OpenAI-style history into Anthropic Messages shape.

    System messages become the `system` prompt, assistant text/tool calls become
    `text`/`tool_use` blocks, and tool results become `tool_result` blocks
    grouped into a single user message. Adjacent same-role messages are merged so
    the result always alternates user/assistant.
    """

    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            content = _text_content(message.get("content"))
            if content.strip():
                system_parts.append(content)
            continue
        if role == "tool":
            raw = message.get("content")
            text = _text_content(raw)
            images = _image_parts(raw)
            result: str | list[dict[str, Any]] = text
            if images:
                result = [{"type": "text", "text": text}] if text else []
                result += _anthropic_images(images)
            _append_blocks(
                converted,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id") or ""),
                        "content": result,
                    }
                ],
            )
            continue
        if role == "assistant":
            verbatim = _anthropic_content(message)
            if verbatim is not None:
                # Signed thinking blocks must be replayed unchanged and in order.
                _append_blocks(converted, "assistant", verbatim)
                continue
            blocks: list[dict[str, Any]] = []
            content = _text_content(message.get("content"))
            if content:
                blocks.append({"type": "text", "text": content})
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for position, call in enumerate(calls):
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function")
                    if not isinstance(function, dict):
                        continue
                    name = str(function.get("name") or "").strip()
                    if not name:
                        continue
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": str(call.get("id") or f"toolu_{position}"),
                            "name": name,
                            "input": _tool_input(function.get("arguments")),
                        }
                    )
            _append_blocks(converted, "assistant", blocks)
            continue
        content = _text_content(message.get("content"))
        user_blocks = [{"type": "text", "text": content}] if content else []
        user_blocks += _anthropic_images(_image_parts(message.get("content")))
        _append_blocks(converted, "user", user_blocks)
    return system_parts, _normalize_anthropic_turns(converted)


def convert_tools_for_anthropic(
    tools: Sequence[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Translate OpenAI function tools into Anthropic tool definitions."""

    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        schema = function.get("parameters")
        converted.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "input_schema": (
                    schema
                    if isinstance(schema, dict) and schema
                    else {"type": "object", "properties": {}}
                ),
            }
        )
    return converted


def convert_tools_for_responses(
    tools: Sequence[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Translate OpenAI function tools into Responses API tool definitions."""

    converted: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        schema = function.get("parameters")
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": (
                    schema
                    if isinstance(schema, dict) and schema
                    else {"type": "object", "properties": {}}
                ),
            }
        )
    return converted


def convert_messages_for_responses(
    messages: Sequence[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Translate OpenAI-style history into Responses API input items.

    System messages become `instructions`; assistant tool calls become
    `function_call` items and tool results become `function_call_output` items,
    which is the stateless shape the Responses API expects.
    """

    system_parts: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            content = _text_content(message.get("content"))
            if content:
                system_parts.append(content)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            if not call_id:
                continue
            output = _text_content(message.get("content"))
            dropped = len(_image_parts(message.get("content")))
            if dropped:
                output += f"\n[{dropped} image(s) not sent: unsupported on this protocol]"
            items.append(
                {"type": "function_call_output", "call_id": call_id, "output": output}
            )
            continue
        if role == "assistant":
            content = _text_content(message.get("content"))
            if content:
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name") or "").strip()
                call_id = str(call.get("id") or "").strip()
                if not name or not call_id:
                    continue
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
            continue
        content = _text_content(message.get("content"))
        if content:
            items.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": content}],
                }
            )
    return "\n\n".join(system_parts), items


_GOOGLE_SCHEMA_DROP = frozenset(
    {
        "$defs",
        "$schema",
        "additionalProperties",
        "const",
        "definitions",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "examples",
        "pattern",
        "default",
    }
)


def _clean_google_schema(value: Any) -> Any:
    """Keep only the OpenAPI subset the Gemini Schema object accepts.

    `properties` is a map of caller-chosen names, not schema keywords: a tool
    argument named `pattern` or `default` must survive even though the schema
    keywords of the same name are dropped.
    """

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key == "properties" and isinstance(item, dict):
                cleaned[key] = {
                    name: _clean_google_schema(schema) for name, schema in item.items()
                }
                continue
            if key in _GOOGLE_SCHEMA_DROP:
                continue
            cleaned[key] = _clean_google_schema(item)
        return cleaned
    if isinstance(value, list):
        return [_clean_google_schema(item) for item in value]
    return value


def convert_tools_for_google(
    tools: Sequence[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Translate OpenAI function tools into Gemini functionDeclarations."""

    declarations: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        schema = function.get("parameters")
        declaration: dict[str, Any] = {
            "name": name,
            "description": str(function.get("description") or ""),
        }
        if isinstance(schema, dict) and schema:
            declaration["parameters"] = _clean_google_schema(schema)
        declarations.append(declaration)
    return [{"functionDeclarations": declarations}] if declarations else []


def convert_messages_for_google(
    messages: Sequence[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Translate OpenAI-style history into Gemini `contents`.

    Function responses must carry the function name, so the converter tracks
    call ids from the preceding assistant turn. Thought signatures stored in
    `provider_meta` are echoed back on their functionCall part, which Gemini 4
    requires for multi-turn tool use.
    """

    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    pending_names: dict[str, str] = {}

    def append(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": list(parts)})

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            content = _text_content(message.get("content"))
            if content:
                system_parts.append(content)
            continue
        if role == "assistant":
            parts: list[dict[str, Any]] = []
            content = _text_content(message.get("content"))
            if content:
                parts.append({"text": content})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name") or "").strip()
                if not name:
                    continue
                call_id = str(call.get("id") or "").strip() or f"call_{len(pending_names)}"
                part: dict[str, Any] = {
                    "functionCall": {
                        "name": name,
                        "args": _tool_input(function.get("arguments")),
                    }
                }
                meta = call.get("provider_meta")
                google_meta = meta.get("google") if isinstance(meta, dict) else None
                signature = (
                    google_meta.get("thought_signature")
                    if isinstance(google_meta, dict)
                    else None
                )
                if isinstance(signature, str) and signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
                pending_names[call_id] = name
            append("model", parts)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            response_name = pending_names.get(call_id)
            if not response_name:
                continue
            append(
                "user",
                [
                    {
                        "functionResponse": {
                            "name": response_name,
                            "response": {
                                "content": _text_content(message.get("content"))
                            },
                        }
                    },
                    *_google_images(_image_parts(message.get("content"))),
                ],
            )
            continue
        content = _text_content(message.get("content"))
        user_parts = [{"text": content}] if content else []
        user_parts += _google_images(_image_parts(message.get("content")))
        append("user", user_parts)
    return system_parts, contents


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(frozen=True)
class UsageEvent:
    usage: Usage


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    metadata: dict[str, Any] | None = None

    def parsed_arguments(self) -> dict[str, Any]:
        if not self.arguments.strip():
            return {}
        try:
            value = json.loads(self.arguments)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"model returned invalid JSON arguments for tool {self.name}",
                hint="the tool call was not executed; retry the turn",
                details={"tool": self.name, "error": exc.msg},
            ) from exc
        if not isinstance(value, dict):
            raise ProviderError(
                f"model returned non-object arguments for tool {self.name}",
                details={"tool": self.name},
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


@dataclass(frozen=True)
class DoneEvent:
    finish_reason: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    model: str | None = None
    # Turn-level provider state the next request must echo (Claude thinking).
    metadata: dict[str, Any] | None = None


ProviderEvent = TextDelta | ToolCallDelta | UsageEvent | DoneEvent


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MAX_RETRY_DELAY_SECONDS = 15.0


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


def _error_for_status(
    status: int, body: str, *, retry_after: float | None = None
) -> ProviderError:
    excerpt = redact_text(body.strip())[:400]
    details: dict[str, Any] = {
        "status": status,
        "retryable": status in RETRYABLE_STATUS,
    }
    if retry_after is not None:
        details["retry_after"] = retry_after
    if excerpt:
        details["body"] = excerpt
    if status in {401, 403}:
        return ProviderError(
            f"provider authentication failed ({status}); check the API key environment variable",
            hint="set OPENCODE_API_KEY (or provider.api_key_env) before running Panda",
            details=details,
        )
    if status == 429:
        return ProviderError(
            "provider rate limit reached (429); retry after the window resets",
            hint="free models share a small quota; slow down or switch with `panda models`",
            details=details,
        )
    if status == 404:
        return ProviderError(
            "provider returned 404; the model or endpoint is not available",
            hint="check the model id with `panda models` and the provider base URL",
            details=details,
        )
    if status >= 500:
        return ProviderError(
            f"provider is unavailable ({status})",
            hint="retry later; the failure is upstream",
            details=details,
        )
    return ProviderError(
        f"provider rejected the request ({status})",
        hint="inspect the model id and request shape; no retry is attempted",
        details=details,
    )


def _anthropic_turn_metadata(
    blocks: dict[int, dict[str, Any]], tool_parts: dict[int, dict[str, str]]
) -> dict[str, Any] | None:
    """Ordered content of a Claude turn, only when it carries thinking blocks."""

    if not any(
        block.get("type") in {"thinking", "redacted_thinking"} for block in blocks.values()
    ):
        return None
    ordered: list[dict[str, Any]] = []
    for index, block in sorted(blocks.items()):
        kind = block.get("type")
        if kind == "tool_use":
            slot = tool_parts.get(index)
            if not slot or not slot["name"]:
                continue
            ordered.append(
                {
                    "type": "tool_use",
                    "id": slot["id"] or f"call_{index}",
                    "name": slot["name"],
                    "input": _tool_input(slot["arguments"]),
                }
            )
        elif kind == "text":
            # The Messages API rejects empty text blocks.
            if block.get("text"):
                ordered.append(dict(block))
        else:
            ordered.append(dict(block))
    return {"anthropic_content": ordered}


class OpenCodeProvider:
    """Streaming client for OpenCode Go or a generic multi-protocol gateway."""

    def __init__(
        self,
        settings: ProviderSettings | None = None,
        *,
        model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
        api_key: str | None = None,
    ) -> None:
        self.settings = settings or ProviderSettings()
        self.model = model
        self._client = client
        self._owns_client = client is None
        self._api_key = api_key
        self._closed = False
        # Stable per conversation; Go answers 400 MissingSessionID without it.
        self.session_id = str(uuid.uuid4())

    # ---- lifecycle ----
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.timeout_seconds),
                follow_redirects=False,
            )
            self._owns_client = True
            self._closed = False
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
            self._closed = True

    async def __aenter__(self) -> OpenCodeProvider:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---- urls ----
    @property
    def generic(self) -> bool:
        return self.settings.endpoint_profile == ENDPOINT_GENERIC

    @property
    def models_url(self) -> str:
        if self.generic:
            return f"{self.settings.base_url}/models"
        return f"{self.settings.base_url}/v1/models"

    @property
    def chat_url(self) -> str:
        if self.generic:
            return f"{self.settings.base_url}/chat/completions"
        return f"{self.settings.base_url}/openai/v1/chat/completions"

    @property
    def anthropic_url(self) -> str:
        if self.generic:
            base = self.settings.anthropic_base_url or self.settings.base_url
            return f"{base}/v1/messages"
        return f"{self.settings.base_url}/anthropic/v1/messages"

    @property
    def responses_url(self) -> str:
        if self.generic:
            return f"{self.settings.base_url}/responses"
        return f"{self.settings.base_url}/openai/v1/responses"

    def google_url(self, model_id: str) -> str:
        quoted = quote(model_id, safe="")
        if self.generic:
            base = self.settings.google_base_url or self.settings.base_url
            return f"{base}/v1beta/models/{quoted}:streamGenerateContent?alt=sse"
        return (
            f"{self.settings.base_url}/google/v1beta/models/{quoted}"
            ":streamGenerateContent?alt=sse"
        )

    # ---- auth ----
    def api_key(self) -> str | None:
        if self._api_key is not None:
            return self._api_key
        from_env = os.environ.get(self.settings.api_key_env)
        if from_env:
            return from_env
        return get_api_key(self.settings.name)

    def has_api_key(self) -> bool:
        return bool(self.api_key())

    def _require_api_key(self) -> str:
        key = self.api_key()
        if not key:
            raise ProviderError(
                f"{self.settings.api_key_env} is not set and no stored key exists "
                f"for {self.settings.name}",
                hint=(
                    f"run `panda auth set` (or export {self.settings.api_key_env}); "
                    "the key is stored outside the workspace"
                ),
                details={"env": self.settings.api_key_env},
            )
        return key

    def _headers(
        self,
        *,
        session_id: str | None = None,
        anthropic: bool = False,
        google: bool = False,
    ) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if not self.generic:
            # Go rejects generic clients; this pair is required on every call.
            headers["x-opencode-session"] = session_id or self.session_id
        if anthropic:
            headers["anthropic-version"] = ANTHROPIC_VERSION
        key = self.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
            if self.generic and anthropic:
                headers["x-api-key"] = key
            if self.generic and google:
                headers["x-goog-api-key"] = key
        return headers

    # ---- model resolution ----
    def model_id(self, model: str | None = None) -> str:
        selected = (model or self.model).strip()
        if not selected:
            raise ConfigError("no model selected", hint="set model in .panda/config.json")
        provider, name = split_model(selected)
        if not name:
            # A bare model id is accepted; it resolves to the configured provider.
            name = provider
            provider = self.settings.name
        if provider not in accepted_providers(self.settings):
            raise ProviderError(
                f"model provider {provider!r} is not configured",
                hint=(
                    f"this workspace provider is {self.settings.name!r}; "
                    "use `panda models` to pick a supported id"
                ),
                details={"provider": provider, "configured": self.settings.name},
            )
        return name

    def protocol_for(self, model_id: str) -> str:
        """Wire protocol for a model: the configured override, else by prefix.

        The generic profile has no Responses route for `gpt-*` ids (for example
        `gpt-oss-120b`), so those fall back to OpenAI chat.
        """

        if self.settings.protocol != "auto":
            return self.settings.protocol
        protocol = classify_protocol(model_id)
        if self.generic and protocol == PROTOCOL_RESPONSES:
            return PROTOCOL_OPENAI_CHAT
        return protocol

    def _ensure_protocol(self, model_id: str) -> None:
        protocol = self.protocol_for(model_id)
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ProviderError(
                f"model {model_id!r} uses the {protocol} protocol, "
                "which this build does not implement",
                hint="run `panda models` to see supported model ids",
                details={
                    "model": model_id,
                    "protocol": protocol,
                    "supported_protocols": sorted(SUPPORTED_PROTOCOLS),
                },
            )

    # ---- models ----
    async def list_models(self) -> list[ModelInfo]:
        attempts = self.settings.max_retries + 1
        response: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                response = await self.client.get(
                    self.models_url, headers=self._headers()
                )
            except httpx.TimeoutException as exc:
                error = ProviderError(
                    "provider model list timed out",
                    hint="check the network and provider.base_url",
                    details={"retryable": True},
                )
                if attempt + 1 >= attempts:
                    raise error from exc
                await self._sleep_before_retry(attempt, error)
                continue
            except httpx.HTTPError as exc:
                error = ProviderError(
                    "cannot reach the provider model list",
                    hint="check the network and provider.base_url",
                    details={"error": type(exc).__name__, "retryable": True},
                )
                if attempt + 1 >= attempts:
                    raise error from exc
                await self._sleep_before_retry(attempt, error)
                continue
            if response.status_code >= 400:
                error = _error_for_status(
                    response.status_code,
                    response.text,
                    retry_after=_retry_after(response),
                )
                if attempt + 1 < attempts and error.details.get("retryable"):
                    await self._sleep_before_retry(attempt, error)
                    continue
                raise error
            break
        if response is None:  # pragma: no cover - the loop either returns or raises
            raise ProviderError("provider model list failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "provider model list is not JSON",
                details={"status": response.status_code},
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ProviderError("provider model list is missing a data array")
        models: list[ModelInfo] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            created = item.get("created")
            models.append(
                ModelInfo.from_id(
                    model_id,
                    owned_by=str(item.get("owned_by") or self.settings.name),
                    created=int(created) if isinstance(created, (int, float)) else None,
                )
            )
        models.sort(key=lambda model: model.id)
        return models

    # ---- chat ----
    async def stream(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        session_id: str | None = None,
        deadline: float | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one model step.

        By default a retry is only attempted before the first event. With
        `retry_partial`, each attempt is buffered and released only once it
        completes, so a retryable failure mid-stream discards the partial turn
        and the model never sees an incomplete one. `deadline` is a
        `time.monotonic()` value; no retry wait may cross it. With both,
        `max_retries` no longer bounds the attempts: the deadline does.
        `effort` overrides `provider.effort` for this call only (per-phase effort).
        """

        model_id = self.model_id(model)
        self._ensure_protocol(model_id)
        key = self._require_api_key()
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ProviderError(
                f"unknown effort {effort!r}",
                hint="use one of " + ", ".join(EFFORT_LEVELS),
                details={"effort": effort},
            )
        session = session_id or self.session_id
        attempts = self.settings.max_retries + 1
        buffer = self.settings.retry_partial
        # Headless runs with a deadline never give up on a transient failure:
        # rate limits and endpoint outages are retried until the clock runs out.
        until_deadline = buffer and deadline is not None
        attempt = 0
        while True:
            emitted = False
            buffered: list[ProviderEvent] = []
            try:
                async for event in self._stream_once(
                    messages,
                    tools,
                    model_id=model_id,
                    key=key,
                    session_id=session,
                    effort=effort,
                ):
                    if buffer:
                        buffered.append(event)
                        continue
                    emitted = True
                    yield event
            except ProviderError as exc:
                retryable = bool(exc.details.get("retryable")) if exc.details else False
                if emitted or not retryable or (not until_deadline and attempt + 1 >= attempts):
                    raise
                await self._sleep_before_retry(attempt, exc, deadline=deadline)
                attempt += 1
                continue
            for event in buffered:
                yield event
            return

    async def _stream_once(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        *,
        model_id: str,
        key: str,
        session_id: str,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        protocol = self.protocol_for(model_id)
        if protocol == PROTOCOL_ANTHROPIC:
            async for event in self._stream_anthropic(
                messages, tools, model_id=model_id, key=key, session_id=session_id, effort=effort
            ):
                yield event
            return
        if protocol == PROTOCOL_RESPONSES:
            async for event in self._stream_responses(
                messages, tools, model_id=model_id, key=key, session_id=session_id, effort=effort
            ):
                yield event
            return
        if protocol == PROTOCOL_GOOGLE:
            async for event in self._stream_google(
                messages, tools, model_id=model_id, key=key, session_id=session_id, effort=effort
            ):
                yield event
            return
        async for event in self._stream_openai(
            messages, tools, model_id=model_id, key=key, session_id=session_id, effort=effort
        ):
            yield event

    async def _sleep_before_retry(
        self, attempt: int, exc: ProviderError, *, deadline: float | None = None
    ) -> None:
        details = exc.details or {}
        retry_after = details.get("retry_after")
        if isinstance(retry_after, (int, float)) and retry_after >= 0:
            delay = float(retry_after)
        else:
            delay = self.settings.retry_base_seconds * (2**attempt)
        delay = min(delay, self.settings.max_retry_delay_seconds)
        # Jitter spreads retries; it is not a security boundary.
        delay *= random.uniform(0.7, 1.3)  # noqa: S311
        if deadline is not None and time.monotonic() + delay >= deadline:
            # Waiting would eat the time reserved for wrapping up.
            raise exc
        await self._sleep(delay)

    def _effort(self, model_id: str) -> str | None:
        return self.settings.effort

    def _cache_enabled(self, model_id: str) -> bool:
        return self.settings.prompt_cache and is_claude_model(model_id)

    def _incomplete_stream(self, protocol: str) -> ProviderError:
        return ProviderError(
            "provider stream ended without a finish reason",
            hint="the partial turn was discarded",
            details={"protocol": protocol, "retryable": True},
        )

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def _stream_openai(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        *,
        model_id: str,
        key: str,
        session_id: str,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        chat_messages = sanitize_messages_for_openai(messages)
        if self._cache_enabled(model_id):
            chat_messages = _cache_chat_messages(chat_messages)
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": chat_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self.settings.max_output_tokens,
        }
        if not is_claude_model(model_id):
            # Claude answers 400 to any sampling parameter.
            payload["temperature"] = self.settings.temperature
        effort = effort or self._effort(model_id)
        if effort:
            payload["reasoning_effort"] = effort
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        headers = {**self._headers(session_id=session_id), "Authorization": f"Bearer {key}"}
        tool_parts: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        response_model: str | None = None

        try:
            async with self.client.stream(
                "POST", self.chat_url, json=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise _error_for_status(
                        response.status_code, body, retry_after=_retry_after(response)
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            "provider sent a malformed stream chunk",
                            details={"error": exc.msg},
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        raw_error = chunk["error"]
                        message = (
                            raw_error.get("message")
                            if isinstance(raw_error, dict)
                            else str(raw_error)
                        )
                        raise ProviderError(
                            "provider reported an error mid-stream",
                            details={"message": redact_text(str(message))[:400]},
                        )
                    response_model = str(chunk.get("model") or response_model or "") or None
                    usage = parse_usage(chunk.get("usage"))
                    if usage is not None:
                        yield UsageEvent(usage)
                    choices = chunk.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield TextDelta(content)
                    fragments = delta.get("tool_calls")
                    if isinstance(fragments, list):
                        for fragment in fragments:
                            if not isinstance(fragment, dict):
                                continue
                            index = _as_int(fragment.get("index"))
                            slot = tool_parts.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            if fragment.get("id"):
                                slot["id"] = str(fragment["id"])
                            function = fragment.get("function")
                            if isinstance(function, dict):
                                if function.get("name"):
                                    slot["name"] = str(function["name"])
                                if function.get("arguments"):
                                    slot["arguments"] += str(function["arguments"])
                            yield ToolCallDelta(
                                index=index,
                                id=slot["id"] or None,
                                name=slot["name"] or None,
                                arguments=str(
                                    function.get("arguments", "")
                                    if isinstance(function, dict)
                                    else ""
                                ),
                            )
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"provider stream timed out after {self.settings.timeout_seconds:g}s",
                hint="raise provider.timeout_seconds or narrow the task",
                details={"retryable": True},
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "provider stream failed at the transport layer",
                hint="retry once; if it repeats, check the network and provider.base_url",
                details={"error": type(exc).__name__, "retryable": True},
            ) from exc

        if finish_reason is None and self.settings.retry_partial:
            raise self._incomplete_stream(PROTOCOL_OPENAI_CHAT)
        calls = tuple(
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"],
            )
            for index, slot in sorted(tool_parts.items())
            if slot["name"]
        )
        yield DoneEvent(
            finish_reason=finish_reason, tool_calls=calls, model=response_model
        )


    async def _stream_anthropic(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        *,
        model_id: str,
        key: str,
        session_id: str,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream Anthropic Messages, normalized to the OpenAI-chat event shape."""

        system_parts, converted = convert_messages_for_anthropic(messages)
        tool_payload = convert_tools_for_anthropic(tools)
        # Newer Claude models reject temperature ("temperature is deprecated
        # for this model"), so the adapter omits it and lets the API default apply.
        # Claude also rejects budget_tokens, disabled thinking and forced
        # tool_choice, so none is sent; depth is controlled by effort only.
        cache = self._cache_enabled(model_id)
        if cache:
            _mark_last_block_for_cache(converted)
        payload: dict[str, Any] = {
            "model": model_id,
            "max_tokens": self.settings.max_output_tokens,
            "messages": converted,
            "stream": True,
        }
        if system_parts:
            system_text = "\n\n".join(system_parts)
            payload["system"] = (
                [{"type": "text", "text": system_text, "cache_control": dict(EPHEMERAL_CACHE)}]
                if cache
                else system_text
            )
        if tool_payload:
            payload["tools"] = tool_payload
        effort = effort or self._effort(model_id)
        if effort:
            payload["output_config"] = {"effort": effort}
        headers = {**self._headers(session_id=session_id, anthropic=True)}
        headers["Authorization"] = f"Bearer {key}"

        tool_parts: dict[int, dict[str, str]] = {}
        # Every content block of the turn, by stream index, for verbatim replay.
        content_blocks: dict[int, dict[str, Any]] = {}
        usage_raw: dict[str, Any] = {}
        finish_reason: str | None = None
        response_model: str | None = model_id

        try:
            async with self.client.stream(
                "POST", self.anthropic_url, json=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise _error_for_status(
                        response.status_code, body, retry_after=_retry_after(response)
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            "provider sent a malformed stream chunk",
                            details={"error": exc.msg},
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    kind = chunk.get("type")
                    if kind == "error":
                        raw_error = chunk.get("error")
                        message = (
                            raw_error.get("message")
                            if isinstance(raw_error, dict)
                            else raw_error
                        )
                        raise ProviderError(
                            "provider reported an error mid-stream",
                            details={"message": redact_text(str(message))[:400]},
                        )
                    if kind == "message_start":
                        frame = chunk.get("message")
                        if isinstance(frame, dict):
                            response_model = str(frame.get("model") or response_model or "")
                            merge_usage(usage_raw, frame.get("usage"))
                        continue
                    if kind == "content_block_start":
                        index = _as_int(chunk.get("index"))
                        block = chunk.get("content_block")
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "tool_use":
                            content_blocks[index] = {"type": "tool_use"}
                            slot = tool_parts.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            if block.get("id"):
                                slot["id"] = str(block["id"])
                            if block.get("name"):
                                slot["name"] = str(block["name"])
                            yield ToolCallDelta(
                                index=index, id=slot["id"] or None, name=slot["name"] or None
                            )
                        elif block_type == "text":
                            initial = block.get("text")
                            content_blocks[index] = {
                                "type": "text",
                                "text": initial if isinstance(initial, str) else "",
                            }
                            if isinstance(initial, str) and initial:
                                yield TextDelta(initial)
                        elif block_type == "thinking":
                            content_blocks[index] = {
                                "type": "thinking",
                                "thinking": str(block.get("thinking") or ""),
                                "signature": str(block.get("signature") or ""),
                            }
                        elif block_type == "redacted_thinking":
                            content_blocks[index] = copy.deepcopy(block)
                        continue
                    if kind == "content_block_delta":
                        index = _as_int(chunk.get("index"))
                        delta = chunk.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        delta_type = delta.get("type")
                        target = content_blocks.get(index)
                        if delta_type == "text_delta":
                            content = delta.get("text")
                            if isinstance(content, str) and content:
                                if target is not None and target["type"] == "text":
                                    target["text"] += content
                                yield TextDelta(content)
                        elif delta_type == "thinking_delta":
                            if target is not None and target["type"] == "thinking":
                                target["thinking"] += str(delta.get("thinking") or "")
                        elif delta_type == "signature_delta":
                            if target is not None and target["type"] == "thinking":
                                target["signature"] += str(delta.get("signature") or "")
                        elif delta_type == "input_json_delta":
                            fragment = delta.get("partial_json")
                            if isinstance(fragment, str) and fragment:
                                slot = tool_parts.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                slot["arguments"] += fragment
                                yield ToolCallDelta(
                                    index=index,
                                    id=slot["id"] or None,
                                    name=slot["name"] or None,
                                    arguments=fragment,
                                )
                        continue
                    if kind == "message_delta":
                        delta = chunk.get("delta")
                        if isinstance(delta, dict) and delta.get("stop_reason"):
                            raw_reason = str(delta["stop_reason"])
                            finish_reason = ANTHROPIC_STOP_REASONS.get(raw_reason, raw_reason)
                        merge_usage(usage_raw, chunk.get("usage"))
                        continue
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"provider stream timed out after {self.settings.timeout_seconds:g}s",
                hint="raise provider.timeout_seconds or narrow the task",
                details={"retryable": True},
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "provider stream failed at the transport layer",
                hint="retry once; if it repeats, check the network and provider.base_url",
                details={"error": type(exc).__name__, "retryable": True},
            ) from exc

        if finish_reason is None and self.settings.retry_partial:
            raise self._incomplete_stream(PROTOCOL_ANTHROPIC)
        usage = parse_anthropic_usage(usage_raw)
        if usage is not None:
            yield UsageEvent(usage)
        calls = tuple(
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"],
            )
            for index, slot in sorted(tool_parts.items())
            if slot["name"]
        )
        if finish_reason is None:
            finish_reason = "tool_calls" if calls else "stop"
        yield DoneEvent(
            finish_reason=finish_reason,
            tool_calls=calls,
            model=response_model,
            metadata=_anthropic_turn_metadata(content_blocks, tool_parts),
        )


    async def _stream_responses(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        *,
        model_id: str,
        key: str,
        session_id: str,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream the OpenAI Responses API, normalized to the shared events."""

        instructions, converted = convert_messages_for_responses(messages)
        payload: dict[str, Any] = {
            "model": model_id,
            "input": converted,
            "stream": True,
            "max_output_tokens": self.settings.max_output_tokens,
        }
        if instructions:
            payload["instructions"] = instructions
        effort = effort or self._effort(model_id)
        if effort:
            payload["reasoning"] = {"effort": effort}
        if tools:
            tool_payload = convert_tools_for_responses(tools)
            if tool_payload:
                payload["tools"] = tool_payload
                payload["tool_choice"] = "auto"

        headers = {**self._headers(session_id=session_id), "Authorization": f"Bearer {key}"}
        slots: dict[int, dict[str, str]] = {}
        usage_raw: dict[str, Any] = {}
        finish_reason: str | None = None
        response_model: str | None = None

        try:
            async with self.client.stream(
                "POST", self.responses_url, json=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise _error_for_status(
                        response.status_code, body, retry_after=_retry_after(response)
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            "provider sent a malformed stream chunk",
                            details={"error": exc.msg},
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    kind = chunk.get("type")
                    if kind == "error" or kind == "response.failed":
                        raw_error = chunk.get("error")
                        if raw_error is None and isinstance(chunk.get("response"), dict):
                            raw_error = chunk["response"].get("error")
                        message = (
                            raw_error.get("message")
                            if isinstance(raw_error, dict)
                            else raw_error
                        )
                        raise ProviderError(
                            "provider reported an error mid-stream",
                            details={"message": redact_text(str(message))[:400]},
                        )
                    if kind in {"response.created", "response.in_progress"}:
                        frame = chunk.get("response")
                        if isinstance(frame, dict):
                            response_model = (
                                str(frame.get("model") or response_model or "") or None
                            )
                        continue
                    if kind == "response.output_item.added":
                        index = _as_int(chunk.get("output_index"))
                        item = chunk.get("item")
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            slot = slots.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            slot["id"] = str(item.get("call_id") or slot["id"])
                            slot["name"] = str(item.get("name") or slot["name"])
                            yield ToolCallDelta(
                                index=index,
                                id=slot["id"] or None,
                                name=slot["name"] or None,
                            )
                        continue
                    if kind == "response.function_call_arguments.delta":
                        index = _as_int(chunk.get("output_index"))
                        delta = chunk.get("delta")
                        if isinstance(delta, str) and delta:
                            slot = slots.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            slot["arguments"] += delta
                            yield ToolCallDelta(
                                index=index,
                                id=slot["id"] or None,
                                name=slot["name"] or None,
                                arguments=delta,
                            )
                        continue
                    if kind == "response.function_call_arguments.done":
                        index = _as_int(chunk.get("output_index"))
                        arguments = chunk.get("arguments")
                        if isinstance(arguments, str):
                            slot = slots.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            slot["arguments"] = arguments
                        continue
                    if kind == "response.output_item.done":
                        index = _as_int(chunk.get("output_index"))
                        item = chunk.get("item")
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            slot = slots.setdefault(
                                index, {"id": "", "name": "", "arguments": ""}
                            )
                            slot["id"] = str(item.get("call_id") or slot["id"])
                            slot["name"] = str(item.get("name") or slot["name"])
                            slot["arguments"] = str(
                                item.get("arguments") or slot["arguments"]
                            )
                        continue
                    if kind in {"response.output_text.delta", "response.refusal.delta"}:
                        delta = chunk.get("delta")
                        if isinstance(delta, str) and delta:
                            yield TextDelta(delta)
                        continue
                    if kind in {"response.completed", "response.incomplete"}:
                        frame = chunk.get("response")
                        if not isinstance(frame, dict):
                            continue
                        response_model = (
                            str(frame.get("model") or response_model or "") or None
                        )
                        merge_usage(usage_raw, frame.get("usage"))
                        details = frame.get("incomplete_details")
                        if kind == "response.incomplete":
                            reason = (
                                str(details.get("reason") or "")
                                if isinstance(details, dict)
                                else ""
                            )
                            finish_reason = (
                                "length" if reason == "max_output_tokens" else "incomplete"
                            )
                        continue
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"provider stream timed out after {self.settings.timeout_seconds:g}s",
                hint="raise provider.timeout_seconds or narrow the task",
                details={"retryable": True},
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "provider stream failed at the transport layer",
                hint="retry once; if it repeats, check the network and provider.base_url",
                details={"error": type(exc).__name__, "retryable": True},
            ) from exc

        usage = parse_responses_usage(usage_raw)
        if usage is not None:
            yield UsageEvent(usage)
        calls = tuple(
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"],
            )
            for index, slot in sorted(slots.items())
            if slot["name"]
        )
        if finish_reason is None:
            finish_reason = "tool_calls" if calls else "stop"
        yield DoneEvent(finish_reason=finish_reason, tool_calls=calls, model=response_model)

    async def _stream_google(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        *,
        model_id: str,
        key: str,
        session_id: str,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream Gemini generateContent, normalized to the shared events."""

        system_parts, contents = convert_messages_for_google(messages)
        generation: dict[str, Any] = {"maxOutputTokens": self.settings.max_output_tokens}
        if not is_claude_model(model_id):
            generation["temperature"] = self.settings.temperature
        # No thinkingConfig: Gemini 4 Argon answers 400 to any thinkingBudget.
        payload: dict[str, Any] = {"contents": contents, "generationConfig": generation}
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if tools:
            tool_payload = convert_tools_for_google(tools)
            if tool_payload:
                payload["tools"] = tool_payload

        headers = {
            **self._headers(session_id=session_id, google=True),
            "Authorization": f"Bearer {key}",
        }
        slots: dict[int, dict[str, str]] = {}
        signatures: dict[str, str] = {}
        usage_raw: dict[str, Any] = {}
        finish_reason: str | None = None
        response_model: str | None = None
        next_index = 0

        try:
            async with self.client.stream(
                "POST", self.google_url(model_id), json=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise _error_for_status(
                        response.status_code, body, retry_after=_retry_after(response)
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            "provider sent a malformed stream chunk",
                            details={"error": exc.msg},
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    raw_error = chunk.get("error")
                    if raw_error is not None:
                        message = (
                            raw_error.get("message")
                            if isinstance(raw_error, dict)
                            else raw_error
                        )
                        raise ProviderError(
                            "provider reported an error mid-stream",
                            details={"message": redact_text(str(message))[:400]},
                        )
                    feedback = chunk.get("promptFeedback")
                    if isinstance(feedback, dict) and feedback.get("blockReason"):
                        raise ProviderError(
                            "provider blocked the prompt",
                            details={"reason": str(feedback["blockReason"])},
                        )
                    response_model = str(chunk.get("modelVersion") or response_model or "") or None
                    merge_usage(usage_raw, chunk.get("usageMetadata"))
                    candidates = chunk.get("candidates")
                    if not isinstance(candidates, list):
                        continue
                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            continue
                        content = candidate.get("content")
                        parts = content.get("parts") if isinstance(content, dict) else None
                        for part in parts if isinstance(parts, list) else []:
                            if not isinstance(part, dict) or part.get("thought") is True:
                                continue
                            text = part.get("text")
                            if isinstance(text, str) and text:
                                yield TextDelta(text)
                            call = part.get("functionCall")
                            if not isinstance(call, dict):
                                continue
                            name = str(call.get("name") or "").strip()
                            if not name:
                                continue
                            call_id = str(call.get("id") or "").strip() or (
                                f"call_{next_index}"
                            )
                            if any(slot["id"] == call_id for slot in slots.values()):
                                continue
                            index = next_index
                            next_index += 1
                            arguments = json.dumps(
                                _tool_input(call.get("args")),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            slots[index] = {
                                "id": call_id,
                                "name": name,
                                "arguments": arguments,
                            }
                            signature = part.get("thoughtSignature") or part.get(
                                "thought_signature"
                            )
                            if isinstance(signature, str) and signature:
                                signatures[call_id] = signature
                            yield ToolCallDelta(
                                index=index,
                                id=call_id,
                                name=name,
                                arguments=arguments,
                            )
                        reason = candidate.get("finishReason")
                        if reason:
                            raw_reason = str(reason)
                            if raw_reason == "MALFORMED_FUNCTION_CALL":
                                raise ProviderError(
                                    "provider could not form a valid tool call",
                                    hint="retry the turn",
                                    details={"finish_reason": raw_reason},
                                )
                            finish_reason = GOOGLE_FINISH_REASONS.get(
                                raw_reason, raw_reason.lower()
                            )
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"provider stream timed out after {self.settings.timeout_seconds:g}s",
                hint="raise provider.timeout_seconds or narrow the task",
                details={"retryable": True},
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "provider stream failed at the transport layer",
                hint="retry once; if it repeats, check the network and provider.base_url",
                details={"error": type(exc).__name__, "retryable": True},
            ) from exc

        usage = parse_google_usage(usage_raw)
        if usage is not None:
            yield UsageEvent(usage)
        calls = tuple(
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"],
                metadata=(
                    {"google": {"thought_signature": signatures[slot["id"]]}}
                    if slot["id"] in signatures
                    else None
                ),
            )
            for index, slot in sorted(slots.items())
            if slot["name"]
        )
        if finish_reason is None:
            finish_reason = "tool_calls" if calls else "stop"
        elif calls and finish_reason == "stop":
            finish_reason = "tool_calls"
        yield DoneEvent(finish_reason=finish_reason, tool_calls=calls, model=response_model)


def provider_for_config(config: PandaConfig) -> OpenCodeProvider:
    return OpenCodeProvider(config.provider, model=config.model)


def status_snapshot(provider: OpenCodeProvider | None = None) -> dict[str, Any]:
    """Non-secret provider status for doctor/status. Never returns the key."""

    settings = provider.settings if provider else ProviderSettings()
    present = (
        provider.has_api_key()
        if provider
        else bool(
            os.environ.get(settings.api_key_env) or get_api_key(settings.name)
        )
    )
    return {
        "name": settings.name,
        "base_url": settings.base_url,
        "api_key_env": settings.api_key_env,
        "api_key_present": present,
        "model": provider.model if provider else None,
    }


__all__ = [
    "ANTHROPIC_VERSION",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_PROVIDER",
    "DoneEvent",
    "ModelInfo",
    "OpenCodeProvider",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_GOOGLE",
    "PROTOCOL_OPENAI_CHAT",
    "PROTOCOL_RESPONSES",
    "ProviderEvent",
    "SUPPORTED_PROTOCOLS",
    "TextDelta",
    "ToolCall",
    "ToolCallDelta",
    "Usage",
    "UsageEvent",
    "USER_AGENT",
    "classify_protocol",
    "convert_messages_for_anthropic",
    "convert_messages_for_google",
    "convert_messages_for_responses",
    "convert_tools_for_anthropic",
    "convert_tools_for_google",
    "convert_tools_for_responses",
    "is_claude_model",
    "is_free_model",
    "is_supported_model",
    "model_protocol",
    "parse_anthropic_usage",
    "parse_google_usage",
    "parse_responses_usage",
    "parse_usage",
    "provider_for_config",
    "sanitize_messages_for_openai",
    "status_snapshot",
]