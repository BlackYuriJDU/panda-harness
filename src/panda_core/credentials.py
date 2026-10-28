"""User-scoped credential store for provider API keys.

Workspace configs never hold secrets. Resolution order is:

1. an explicit key passed to the provider (tests, embeddings);
2. the provider's environment variable (``OPENCODE_API_KEY`` by default);
3. this store: ``%LOCALAPPDATA%\\panda\\credentials.json`` on Windows,
   ``$XDG_DATA_HOME/panda/credentials.json`` elsewhere.

``PANDA_CREDENTIALS_FILE`` overrides the path (used by tests and portable
installs). Reads are lock-free: writers replace the file atomically.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from panda_core.atomic import atomic_write_json, file_lock
from panda_core.errors import ConfigError, UsageError

CREDENTIALS_ENV = "PANDA_CREDENTIALS_FILE"
_FILE_NAME = "credentials.json"
_DIR_NAME = "panda"
_SCHEMA_VERSION = 1
_MAX_KEY_LENGTH = 4096
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def credentials_path() -> Path:
    """Return the credential store path without creating it."""

    override = os.environ.get(CREDENTIALS_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if base:
            return Path(base) / _DIR_NAME / _FILE_NAME
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if data_home:
        return Path(data_home) / _DIR_NAME / _FILE_NAME
    return Path.home() / ".local" / "share" / _DIR_NAME / _FILE_NAME


def _validate_provider(provider: str) -> str:
    name = provider.strip()
    if not _PROVIDER_RE.match(name):
        raise UsageError(
            f"invalid provider name: {provider!r}",
            hint="use the provider name from .panda/config.json",
        )
    return name


def _validate_key(key: str) -> str:
    text = key.strip()
    if not text:
        raise UsageError("the API key is empty")
    if any(char.isspace() for char in text):
        raise UsageError("the API key must not contain whitespace")
    if len(text) > _MAX_KEY_LENGTH:
        raise UsageError("the API key is too long")
    return text


def _read_keys(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(
            f"cannot read the credential store: {path}",
            hint="check the file permissions or run `panda auth clear`",
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"the credential store is not valid JSON: {path}",
            hint="run `panda auth clear` and set the key again",
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(
            f"the credential store has an unexpected shape: {path}",
            hint="run `panda auth clear` and set the key again",
        )
    keys = raw.get("keys", {})
    if not isinstance(keys, dict):
        raise ConfigError(
            f"the credential store has an unexpected shape: {path}",
            hint="run `panda auth clear` and set the key again",
        )
    return {
        str(name): value
        for name, value in keys.items()
        if isinstance(value, str) and value.strip()
    }


def get_api_key(provider: str) -> str | None:
    """Return the stored key for ``provider``, or ``None``."""

    name = provider.strip()
    if not name:
        return None
    keys = _read_keys(credentials_path())
    value = keys.get(name)
    return value.strip() if value else None


def has_api_key(provider: str) -> bool:
    return get_api_key(provider) is not None


def set_api_key(provider: str, key: str) -> Path:
    """Store ``key`` for ``provider`` and return the file path."""

    name = _validate_provider(provider)
    value = _validate_key(key)
    path = credentials_path()
    with file_lock(path.with_name(f"{path.name}.lock")):
        keys = _read_keys(path)
        keys[name] = value
        atomic_write_json(path, {"version": _SCHEMA_VERSION, "keys": keys})
    return path


def clear_api_key(provider: str) -> bool:
    """Remove one provider key. Returns True when a key was removed."""

    name = provider.strip()
    path = credentials_path()
    if not path.exists():
        return False
    with file_lock(path.with_name(f"{path.name}.lock")):
        keys = _read_keys(path)
        removed = keys.pop(name, None) is not None
        if removed:
            if keys:
                atomic_write_json(path, {"version": _SCHEMA_VERSION, "keys": keys})
            else:
                path.unlink(missing_ok=True)
    return removed


def clear_all() -> bool:
    """Remove every stored key. Returns True when the file existed."""

    path = credentials_path()
    if not path.exists():
        return False
    with file_lock(path.with_name(f"{path.name}.lock")):
        path.unlink(missing_ok=True)
    return True


def mask_key(key: str | None) -> str:
    """Return a safe display form; never the full key."""

    if not key:
        return "not set"
    tail = key[-4:] if len(key) >= 8 else ""
    return f"set (...{tail})" if tail else "set"
