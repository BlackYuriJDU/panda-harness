"""OpenCode Console provider (https://opencode.ai/inference).

The provider is deliberately narrow: it speaks the OpenAI-compatible chat
completions protocol, which OpenCode Console exposes for DeepSeek, Qwen, Kimi,
GLM, MiniMax, Mistral, and the free models. Claude, GPT, and Gemini use other
protocols on the same base URL; they are classified and reported honestly as
unsupported instead of being called with the wrong adapter.

No secret ever appears in an error message. The API key is read from the
environment by name and only used in the Authorization header.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from panda_core.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    PandaConfig,
    ProviderSettings,
    split_model,
)
from panda_core.errors import ConfigError, ProviderError
from panda_core.redact import redact_text

PROTOCOL_OPENAI_CHAT = "openai-chat"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_RESPONSES = "responses"
PROTOCOL_GOOGLE = "google"
SUPPORTED_PROTOCOLS = frozenset({PROTOCOL_OPENAI_CHAT})
FREE_SUFFIX = "-free"


def classify_protocol(model_id: str) -> str:
    """Map a model id to the wire protocol it requires on OpenCode Console.

    The prefixes are stable in the public catalogue: `claude-*` uses the
    Anthropic messages API, `gpt-*` uses the Responses API, and `gemini-*` uses
    the Google generateContent API. Everything else is OpenAI chat compatible.
    """

    name = model_id.strip().lower()
    if name.startswith("claude"):
        return PROTOCOL_ANTHROPIC
    if name.startswith("gpt") or name.startswith("o1") or name.startswith("o3"):
        return PROTOCOL_RESPONSES
    if name.startswith("gemini"):
        return PROTOCOL_GOOGLE
    return PROTOCOL_OPENAI_CHAT


def is_free_model(model_id: str) -> bool:
    return model_id.strip().lower().endswith(FREE_SUFFIX)


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

    def qualified(self, provider: str = "opencode") -> str:
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
            "cost_usd": self.cost_usd,
        }


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


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
    cost_raw = raw.get("cost", raw.get("total_cost", raw.get("cost_usd")))
    cost: float | None = None
    if cost_raw is not None:
        try:
            cost = float(cost_raw)
        except (TypeError, ValueError):
            cost = None
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_tokens=cached,
        cost_usd=cost,
    )


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
    """Streaming client for OpenCode Console's OpenAI-compatible endpoint."""

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

    # ---- auth ----
    def api_key(self) -> str | None:
        if self._api_key is not None:
            return self._api_key
        return os.environ.get(self.settings.api_key_env)

    def has_api_key(self) -> bool:
        return bool(self.api_key())

    def _require_api_key(self) -> str:
        key = self.api_key()
        if not key:
            raise ProviderError(
                f"{self.settings.api_key_env} is not set",
                hint=(
                    f"export {self.settings.api_key_env} in this shell; "
                    "Panda never reads or stores the value"
                ),
                details={"env": self.settings.api_key_env},
            )
        return key

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
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
        if provider != self.settings.name:
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
                    "only OpenAI-compatible chat models are callable today; "
                    "choose a DeepSeek/Qwen/Kimi/GLM/MiniMax/Mistral model "
                    "(the picker marks the others unsupported)"
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
    ) -> AsyncIterator[ProviderEvent]:
        model_id = self.model_id(model)
        self._ensure_protocol(model_id)
        key = self._require_api_key()
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

        headers = {**self._headers(), "Authorization": f"Bearer {key}"}
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


def provider_for_config(config: PandaConfig) -> OpenCodeProvider:
    return OpenCodeProvider(config.provider, model=config.model)


def model_protocol(model: str) -> str:
    provider, name = split_model(model)
    return classify_protocol(name or provider)


def is_supported_model(model: str) -> bool:
    return model_protocol(model) in SUPPORTED_PROTOCOLS


def status_snapshot(provider: OpenCodeProvider | None = None) -> dict[str, Any]:
    """Non-secret provider status for doctor/status. Never returns the key."""

    settings = provider.settings if provider else ProviderSettings()
    present = (provider.has_api_key() if provider else bool(os.environ.get(settings.api_key_env)))
    return {
        "name": settings.name,
        "base_url": settings.base_url,
        "api_key_env": settings.api_key_env,
        "api_key_present": present,
        "model": provider.model if provider else None,
    }


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
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
    "classify_protocol",
    "is_free_model",
    "is_supported_model",
    "model_protocol",
    "parse_usage",
    "provider_for_config",
    "status_snapshot",
]