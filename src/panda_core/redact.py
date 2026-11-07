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

# `NAME=value`, `NAME: value`, `"NAME": "value"` where NAME contains a credential word
# as a whole part of the identifier (`DB_PASSWORD`, `client_secret`, `"password"`), not
# inside an ordinary word (`Secretion`, `Tokenization`: R2-219). Found by a linear scan
# from each keyword, never a backtracking pattern over the whole text (R2-220).
_KEYWORD_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|passphrase|pwd"
    r"|private[_-]?key|credential|authorization|cookie)s?(?![a-z])"
)
_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
_SEP_RE = re.compile(r"[\"']?[ \t]{0,3}[=:][ \t]{0,3}([\"']?)")
_VALUE_RE = re.compile(r"[^&\s\"',;}]{6,}")
_NOT_SECRET_NAMES = frozenset({"pwd", "oldpwd"})

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
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bpypi-[A-Za-z0-9_-]{50,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/]{8,}={0,2}"),
    # Any PEM private key, PGP key block or encrypted key; a block cut by an output
    # cap (no END line) is removed up to the end of the text.
    re.compile(
        r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----.*?(?:-----END \1-----|\Z)",
        re.DOTALL,
    ),
)
# `scheme://user:password@host`: the password goes, the rest stays readable.
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,20}://[^\s/:@]{1,128}):([^\s/@]{1,512})@")
_COOKIE_RE = re.compile(r"(?im)^((?:set-)?cookie:[ \t]*)\S.*$")


def _is_secret_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SAFE_COUNT_KEYS:
        return False
    return bool(_SECRET_KEY_RE.search(normalized))


def _redact_assignments(text: str) -> str:
    out: list[str] = []
    cursor = 0
    for match in _KEYWORD_RE.finditer(text):
        if match.start() < cursor:
            continue
        name_start, name_end = match.start(), match.end()
        floor, ceiling = max(0, name_start - 40), min(len(text), name_end + 40)
        while name_start > floor and text[name_start - 1] in _NAME_CHARS:
            name_start -= 1
        while name_end < ceiling and text[name_end] in _NAME_CHARS:
            name_end += 1
        name = text[name_start:name_end].lower().replace("-", "_").strip("._")
        if name in _SAFE_COUNT_KEYS or name in _NOT_SECRET_NAMES:
            continue
        sep = _SEP_RE.match(text, name_end)
        if sep is None:
            continue
        value = _VALUE_RE.match(text, sep.end())
        if value is None:
            continue
        out.append(text[cursor : value.start()])
        out.append(REDACTED)
        cursor = value.end()
    out.append(text[cursor:])
    return "".join(out)


def redact_text(value: str) -> str:
    text = value
    for secret in sorted(_registered, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _SECRET_VALUE_RES:
        text = pattern.sub(REDACTED, text)
    text = _URL_USERINFO_RE.sub(rf"\1:{REDACTED}@", text)
    text = _COOKIE_RE.sub(rf"\1{REDACTED}", text)
    if "=" not in text and ":" not in text:
        return text  # an assignment needs a separator
    return _redact_assignments(text)


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

