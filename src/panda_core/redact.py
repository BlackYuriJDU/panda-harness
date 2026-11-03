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

# `NAME=value`, `NAME: value`, `NAME = "value"` where NAME contains a credential word.
# The name may be wrapped in other words (`DB_PASSWORD`, `AWS_SECRET_ACCESS_KEY`), which
# a `\b` before the word never matched because `_` is a word character (SEC-004).
_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<name>[A-Za-z0-9_.-]{0,40}?(?:api[_-]?key|access[_-]?key|secret|token|password"
    r"|passwd|private[_-]?key|credential)[A-Za-z0-9_.-]{0,40})(?P<sep>[ \t]{0,3}[=:][ \t]{0,3})"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^&\s\"']{6,})"
)

_registered: set[str] = set()


def register_secret(value: str) -> None:
    """Remember a credential this process holds, so it is removed wherever it appears.

    Pattern matching cannot know every key format; the exact value of the key the
    harness loaded can be removed whatever it looks like. Short values are ignored.
    """

    if len(value) >= 8:
        _registered.add(value)


_SECRET_VALUE_RES = (
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\btvly-[A-Za-z0-9]{20,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bgh[ousr]_[A-Za-z0-9]{20,}\b"),
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


def _redact_assignment(match: re.Match[str]) -> str:
    name = match.group("name")
    if name.lower().replace("-", "_") in _SAFE_COUNT_KEYS:
        return match.group(0)
    return f"{name}{match.group('sep')}{match.group('quote')}{REDACTED}"


def redact_text(value: str) -> str:
    text = value
    for secret in sorted(_registered, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _SECRET_VALUE_RES:
        text = pattern.sub(REDACTED, text)
    if "=" not in text and ":" not in text:
        return text  # the assignment pattern needs a separator
    return _ASSIGNMENT_RE.sub(_redact_assignment, text)


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

