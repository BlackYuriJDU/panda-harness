"""Typed workspace configuration (`.panda/config.json`).

Schema v2 replaces the OpenCode runtime with a direct provider (OpenCode
Console). The legacy `opencode` section is still parsed so old workspace files
load without migration, but the runtime never reads it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from panda_core.errors import ConfigError
from panda_core.version import __version__

DEFAULT_MODEL = "opencode/deepseek-v4.1-flash"
DEFAULT_PROVIDER = "opencode"
DEFAULT_BASE_URL = "https://opencode.ai/inference"
DEFAULT_API_KEY_ENV = "OPENCODE_API_KEY"
DEFAULT_CONTEXT_WINDOW = 131_072
DEFAULT_MAX_OUTPUT_TOKENS = 8_192
DEFAULT_VALIDATOR = "corpus/validation/validate.mjs"
DEFAULT_POLICY = ".panda/policy.json"
DEFAULT_SESSIONS = ".panda/sessions"
DEFAULT_INSTRUCTIONS = ".panda/instructions.md"
DEFAULT_OPENCODE_CONFIG = "opencode.json"

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def split_model(model: str) -> tuple[str, str]:
    """Split `provider/model` without assuming the model id has no slashes."""

    provider, _, name = model.partition("/")
    return provider, name


def _relative_posix_path(value: str) -> str:
    """Normalize a configured path and reject anything outside the workspace.

    ``PurePath`` is platform-dependent: on POSIX, ``C:/tmp/x`` is a relative
    path. Workspace configs must be portable, so accept both separators, reject
    drive-qualified and UNC paths, and reject any ``..`` segment.
    """

    text = value.replace("\\", "/")
    if not text.strip():
        raise ValueError("path must not be empty")
    if len(text) >= 2 and text[1] == ":":
        raise ValueError("path must be relative and stay inside the workspace")
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("path must be relative and stay inside the workspace")
    return candidate.as_posix()


class ProviderSettings(BaseModel):
    """OpenCode Console provider settings.

    Only `base_url`, `api_key_env`, and decoding limits are configured here.
    The API key itself is never stored in the workspace.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default=DEFAULT_PROVIDER, pattern=r"^[a-z][a-z0-9-]{1,31}$")
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout_seconds: float = Field(default=120.0, ge=1, le=3600)
    context_window: int = Field(default=DEFAULT_CONTEXT_WINDOW, ge=1024, le=10_000_000)
    max_output_tokens: int = Field(default=DEFAULT_MAX_OUTPUT_TOKENS, ge=1, le=1_000_000)
    temperature: float = Field(default=0.2, ge=0, le=2)

    @field_validator("base_url")
    @classmethod
    def _base_url_is_valid(cls, value: str) -> str:
        text = value.strip().rstrip("/")
        parts = urlsplit(text)
        if not parts.scheme or not parts.hostname:
            raise ValueError("provider.base_url must be an absolute URL")
        if parts.scheme not in {"https", "http"}:
            raise ValueError("provider.base_url must use http or https")
        if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
            raise ValueError("provider.base_url must use https outside loopback")
        if parts.query or parts.fragment:
            raise ValueError("provider.base_url must not have a query or fragment")
        return text

    @field_validator("api_key_env")
    @classmethod
    def _env_name_is_valid(cls, value: str) -> str:
        if not _ENV_NAME_RE.match(value):
            raise ValueError("provider.api_key_env must be an environment variable name")
        return value


class OpencodeSettings(BaseModel):
    """Legacy OpenCode section. Parsed for compatibility, never used at runtime."""

    model_config = ConfigDict(extra="forbid")

    binary: str | list[str] = "opencode"
    agent: str = "panda"
    config_path: str = DEFAULT_OPENCODE_CONFIG
    standalone: bool = True
    panda_command: list[str] = Field(default_factory=lambda: ["panda"])

    @field_validator("binary")
    @classmethod
    def _binary_not_empty(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str) and not value.strip():
            raise ValueError("opencode.binary must not be empty")
        if isinstance(value, list) and (not value or not all(str(part).strip() for part in value)):
            raise ValueError("opencode.binary list must be non-empty")
        return value

    @field_validator("config_path")
    @classmethod
    def _config_path_relative(cls, value: str) -> str:
        return _relative_posix_path(value)

    @field_validator("panda_command")
    @classmethod
    def _panda_command_not_empty(cls, value: list[str]) -> list[str]:
        if not value or not all(str(part).strip() for part in value):
            raise ValueError("opencode.panda_command must be a non-empty command")
        return value


class TimeoutSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validate_seconds: int = Field(default=180, ge=1, le=3600)
    run_seconds: int | None = Field(default=None, ge=1)


class LoggingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redact: bool = True
    log_tasks: bool = True


class PandaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = 2
    model: str = DEFAULT_MODEL
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    validator: str = DEFAULT_VALIDATOR
    node: str = "node"
    node_min_major: int = Field(default=20, ge=0, le=99)
    opencode: OpencodeSettings = Field(default_factory=OpencodeSettings)
    timeouts: TimeoutSettings = Field(default_factory=TimeoutSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    instructions: str = DEFAULT_INSTRUCTIONS
    created_with: str = __version__

    @field_validator("model")
    @classmethod
    def _model_has_provider(cls, value: str) -> str:
        provider, name = split_model(value)
        if not provider or not name.strip() or " " in value:
            raise ValueError("model must use the provider/model form")
        return value

    @field_validator("validator", "instructions")
    @classmethod
    def _relative_inside_workspace(cls, value: str) -> str:
        return _relative_posix_path(value)

    def to_jsonable(self) -> dict:
        return self.model_dump(mode="json")


def load_config(path: Path) -> PandaConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config: {path}", hint="run `panda init`") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in {path}: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    try:
        config = PandaConfig.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"invalid Panda config in {path}: {exc}") from exc
    if config.schema_version == 1:
        # v1 files stay loadable; the next `panda init` rewrites them as v2.
        config = config.model_copy(update={"schema_version": 2})
    return config


def default_config() -> PandaConfig:
    return PandaConfig()