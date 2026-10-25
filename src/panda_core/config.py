"""Typed workspace configuration (`.panda/config.json`)."""

from __future__ import annotations

from pathlib import Path, PurePath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from panda_core.errors import ConfigError
from panda_core.version import __version__

DEFAULT_MODEL = "openrouter/anthropic/claude-sonnet-5.5"
DEFAULT_VALIDATOR = "corpus/validation/validate.mjs"
DEFAULT_POLICY = ".panda/policy.json"
DEFAULT_SESSIONS = ".panda/sessions"
DEFAULT_INSTRUCTIONS = ".panda/instructions.md"
DEFAULT_OPENCODE_CONFIG = "opencode.json"


class OpencodeSettings(BaseModel):
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
        candidate = PurePath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("opencode.config_path must be relative and stay inside the workspace")
        return candidate.as_posix()

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

    schema_version: Literal[1] = 1
    model: str = DEFAULT_MODEL
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
        if "/" not in value or value.startswith("/") or value.endswith("/"):
            raise ValueError("model must use the provider/model form")
        return value

    @field_validator("validator", "instructions")
    @classmethod
    def _relative_inside_workspace(cls, value: str) -> str:
        candidate = PurePath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("path must be relative and stay inside the workspace")
        return candidate.as_posix()

    def to_jsonable(self) -> dict:
        return self.model_dump(mode="json")


def load_config(path: Path) -> PandaConfig:
    import json

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config: {path}", hint="run `panda init`") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in {path}: {exc.msg} (line {exc.lineno}, column {exc.colno})"
        ) from exc
    try:
        return PandaConfig.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"invalid Panda config in {path}: {exc}") from exc


def default_config() -> PandaConfig:
    return PandaConfig()
