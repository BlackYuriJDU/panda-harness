"""OpenCode Go provider (https://opencode.ai/inference/go).

Go exposes two wire protocols under one key: OpenAI-compatible chat completions
at `/openai/v1/chat/completions` and Anthropic Messages at
`/anthropic/v1/messages`. Both adapters are implemented here; GPT/Grok/Muse
(Responses API) and Gemini (Google generateContent) are classified and reported
honestly as unsupported instead of being called with the wrong adapter.

Go also requires two non-generic headers on every request: a stable
`x-opencode-session` id per conversation and a `panda-harness/<version>`
User-Agent. Without them the endpoint answers 400 `MissingSessionID`.

No secret ever appears in an error message. The API key comes from the
environment or the user-scoped credential store (never the workspace) and is
only used in the Authorization header.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from panda_core.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
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
            _append_blocks(
                converted,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id") or ""),
                        "content": _text_content(message.get("content")),
                    }
                ],
            )
            continue
        if role == "assistant":
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
        _append_blocks(converted, "user", [{"type": "text", "text": content}] if content else [])
    return system_parts, converted


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
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True)
class DoneEvent:
    finish_reason: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    model: str | None = None


ProviderEvent = TextDelta | ToolCallDelta | UsageEvent | DoneEvent


def _error_for_status(status: int, body: str) -> ProviderError:
    excerpt = redact_text(body.strip())[:400]
    details: dict[str, Any] = {"status": status}
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


class OpenCodeProvider:
    """Streaming client for OpenCode Go (OpenAI chat + Anthropic Messages)."""

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
    def models_url(self) -> str:
        return f"{self.settings.base_url}/v1/models"

    @property
    def chat_url(self) -> str:
        return f"{self.settings.base_url}/openai/v1/chat/completions"

    @property
    def anthropic_url(self) -> str:
        return f"{self.settings.base_url}/anthropic/v1/messages"

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
        self, *, session_id: str | None = None, anthropic: bool = False
    ) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            # Go rejects generic clients; this pair is required on every call.
            "User-Agent": USER_AGENT,
            "x-opencode-session": session_id or self.session_id,
        }
        if anthropic:
            headers["anthropic-version"] = ANTHROPIC_VERSION
        key = self.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
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

    def _ensure_protocol(self, model_id: str) -> None:
        protocol = classify_protocol(model_id)
        if protocol not in SUPPORTED_PROTOCOLS:
            raise ProviderError(
                f"model {model_id!r} uses the {protocol} protocol, "
                "which this build does not implement",
                hint=(
                    "OpenAI-chat and Anthropic (Claude) models are callable; "
                    "GPT/Grok/Muse need the Responses API and Gemini needs the "
                    "Google API, which this build does not implement"
                ),
                details={
                    "model": model_id,
                    "protocol": protocol,
                    "supported_protocols": sorted(SUPPORTED_PROTOCOLS),
                },
            )

    # ---- models ----
    async def list_models(self) -> list[ModelInfo]:
        try:
            response = await self.client.get(self.models_url, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "provider model list timed out",
                hint="check the network and provider.base_url",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "cannot reach the provider model list",
                hint="check the network and provider.base_url",
                details={"error": type(exc).__name__},
            ) from exc
        if response.status_code >= 400:
            raise _error_for_status(response.status_code, response.text)
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
    ) -> AsyncIterator[ProviderEvent]:
        model_id = self.model_id(model)
        self._ensure_protocol(model_id)
        key = self._require_api_key()
        session = session_id or self.session_id
        if classify_protocol(model_id) == PROTOCOL_ANTHROPIC:
            async for event in self._stream_anthropic(
                messages, tools, model_id=model_id, key=key, session_id=session
            ):
                yield event
            return
        async for event in self._stream_openai(
            messages, tools, model_id=model_id, key=key, session_id=session
        ):
            yield event

    async def _stream_openai(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        *,
        model_id: str,
        key: str,
        session_id: str,
    ) -> AsyncIterator[ProviderEvent]:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": list(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self.settings.max_output_tokens,
            "temperature": self.settings.temperature,
        }
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
                    raise _error_for_status(response.status_code, body)
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
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "provider stream failed at the transport layer",
                hint="retry once; if it repeats, check the network and provider.base_url",
                details={"error": type(exc).__name__},
            ) from exc

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
    ) -> AsyncIterator[ProviderEvent]:
        """Stream Anthropic Messages, normalized to the OpenAI-chat event shape."""

        system_parts, converted = convert_messages_for_anthropic(messages)
        tool_payload = convert_tools_for_anthropic(tools)
        # Newer Claude models reject temperature ("temperature is deprecated
        # for this model"), so the adapter omits it and lets the API default apply.
        payload: dict[str, Any] = {
            "model": model_id,
            "max_tokens": self.settings.max_output_tokens,
            "messages": converted,
            "stream": True,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tool_payload:
            payload["tools"] = tool_payload
        headers = {**self._headers(session_id=session_id, anthropic=True)}
        headers["Authorization"] = f"Bearer {key}"

        tool_parts: dict[int, dict[str, str]] = {}
        usage_raw: dict[str, Any] = {}
        finish_reason: str | None = None
        response_model: str | None = model_id

        try:
            async with self.client.stream(
                "POST", self.anthropic_url, json=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise _error_for_status(response.status_code, body)
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
                        if block.get("type") == "tool_use":
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
                        elif block.get("type") == "text":
                            initial = block.get("text")
                            if isinstance(initial, str) and initial:
                                yield TextDelta(initial)
                        continue
                    if kind == "content_block_delta":
                        index = _as_int(chunk.get("index"))
                        delta = chunk.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            content = delta.get("text")
                            if isinstance(content, str) and content:
                                yield TextDelta(content)
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
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "provider stream failed at the transport layer",
                hint="retry once; if it repeats, check the network and provider.base_url",
                details={"error": type(exc).__name__},
            ) from exc

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
    "convert_tools_for_anthropic",
    "is_free_model",
    "is_supported_model",
    "model_protocol",
    "parse_anthropic_usage",
    "parse_usage",
    "provider_for_config",
    "status_snapshot",
]