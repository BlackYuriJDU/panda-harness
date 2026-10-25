"""Secret redaction for session logs, command traces, and error payloads.

The rule is deliberately conservative: it is better to lose a diagnostic value
than to persist a credential. Nothing in this module reads the environment.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|access_?key|secret|token|password|passwd|authorization|"
    r"credential|credentials|cookie|private_?key)($|_)",
    re.IGNORECASE,
)

# Metrics frequently contain "tokens"; these are counts, never credentials.
_SAFE_COUNT_KEYS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "max_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "window_tokens",
    "used_tokens",
    "available_tokens",
}

_SECRET_VALUE_RES = (
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\btvly-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)=([^&\s\"']{6,})"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def _is_secret_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SAFE_COUNT_KEYS:
        return False
    return bool(_SECRET_KEY_RE.search(normalized))


def redact_text(value: str) -> str:
    text = value
    for pattern in _SECRET_VALUE_RES:
        if pattern.pattern.startswith("(?i)\\b(api"):
            text = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any) -> Any:
    """Return a redacted deep copy. Never mutates the input."""

    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_secret_key(key):
                result[key] = REDACTED
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_env_names(names: list[str] | tuple[str, ...]) -> list[str]:
    """Environment variable names are safe to log; values never are."""

    return sorted({str(name) for name in names})
