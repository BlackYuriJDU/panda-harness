"""Wire-protocol classification for OpenCode Go model ids.

Kept dependency-free: the CLI validates a model before the provider (and httpx)
is imported, so startup does not pay for the HTTP stack before the first frame.
"""

from __future__ import annotations

PROTOCOL_OPENAI_CHAT = "openai-chat"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_RESPONSES = "responses"
PROTOCOL_GOOGLE = "google"
SUPPORTED_PROTOCOLS = frozenset({PROTOCOL_OPENAI_CHAT, PROTOCOL_ANTHROPIC})
FREE_SUFFIX = "-free"


def classify_protocol(model_id: str) -> str:
    """Map a model id to the wire protocol it requires on OpenCode Go.

    The prefixes are stable in the public catalogue: `claude-*` uses the
    Anthropic Messages API, `gpt-*`/`grok-*`/`muse-*` use the Responses API,
    and `gemini-*` uses the Google generateContent API. Everything else is
    OpenAI chat compatible.
    """

    name = model_id.strip().lower()
    if name.startswith("claude"):
        return PROTOCOL_ANTHROPIC
    if (
        name.startswith("gpt")
        or name.startswith("o1")
        or name.startswith("o3")
        or name.startswith("grok")
        or name.startswith("muse")
    ):
        return PROTOCOL_RESPONSES
    if name.startswith("gemini"):
        return PROTOCOL_GOOGLE
    return PROTOCOL_OPENAI_CHAT


def is_free_model(model_id: str) -> bool:
    return model_id.strip().lower().endswith(FREE_SUFFIX)


def model_protocol(model: str) -> str:
    """Classify a bare model id or a `provider/model` reference."""

    provider, _, name = model.partition("/")
    return classify_protocol(name or provider)


def is_supported_model(model: str) -> bool:
    return model_protocol(model) in SUPPORTED_PROTOCOLS


__all__ = [
    "FREE_SUFFIX",
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_GOOGLE",
    "PROTOCOL_OPENAI_CHAT",
    "PROTOCOL_RESPONSES",
    "SUPPORTED_PROTOCOLS",
    "classify_protocol",
    "is_free_model",
    "is_supported_model",
    "model_protocol",
]
