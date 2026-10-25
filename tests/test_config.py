"""Configuration: typed defaults, path safety, and deterministic JSON."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from panda_core.config import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    OpencodeSettings,
    PandaConfig,
    ProviderSettings,
    load_config,
)
from panda_core.errors import ConfigError


def test_default_config_is_valid() -> None:
    config = PandaConfig()
    assert config.schema_version == 2
    assert config.model == DEFAULT_MODEL == "opencode/deepseek-v4.1-flash"
    assert config.provider.name == DEFAULT_PROVIDER
    assert config.provider.base_url == "https://opencode.ai/inference"
    assert config.provider.api_key_env == "OPENCODE_API_KEY"
    assert config.validator == "corpus/validation/validate.mjs"
    assert config.opencode.standalone is True
    assert config.timeouts.validate_seconds == 180
    assert config.logging.redact is True


def test_model_requires_provider_prefix() -> None:
    with pytest.raises(ValidationError):
        PandaConfig(model="claude-sonnet-5.5")
    with pytest.raises(ValidationError):
        PandaConfig(model="/claude")


def test_validator_must_stay_inside_workspace() -> None:
    with pytest.raises(ValidationError):
        PandaConfig(validator="../outside/validate.mjs")
    with pytest.raises(ValidationError):
        PandaConfig(validator="C:/absolute/validate.mjs")
    with pytest.raises(ValidationError):
        PandaConfig(validator=r"C:\absolute\validate.mjs")
    with pytest.raises(ValidationError):
        PandaConfig(validator="//server/share/validate.mjs")
    with pytest.raises(ValidationError):
        PandaConfig(validator="")


def test_validator_and_instructions_normalize_separators() -> None:
    config = PandaConfig(
        validator=r"corpus\validation\validate.mjs",
        instructions=r".panda\instructions.md",
    )
    assert config.validator == "corpus/validation/validate.mjs"
    assert config.instructions == ".panda/instructions.md"


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PandaConfig.model_validate({"model": DEFAULT_MODEL, "unexpected": True})


def test_opencode_config_path_must_stay_inside_workspace() -> None:
    with pytest.raises(ValidationError):
        OpencodeSettings(config_path="../opencode.json")
    with pytest.raises(ValidationError):
        OpencodeSettings(config_path=r"C:\opencode.json")
    assert OpencodeSettings(config_path=".opencode/opencode.json").config_path == (
        ".opencode/opencode.json"
    )
    assert OpencodeSettings(config_path=r".opencode\opencode.json").config_path == (
        ".opencode/opencode.json"
    )


def test_load_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(PandaConfig().model_dump_json(indent=2), encoding="utf-8")
    loaded = load_config(path)
    assert loaded == PandaConfig()


def test_load_config_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.json")


def test_load_config_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_load_config_invalid_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": "nope"}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_timeouts_are_bounded() -> None:
    with pytest.raises(ValidationError):
        PandaConfig.model_validate({"timeouts": {"validate_seconds": 0}})
    with pytest.raises(ValidationError):
        PandaConfig.model_validate({"timeouts": {"validate_seconds": 99999}})


def test_provider_settings_reject_remote_http() -> None:
    with pytest.raises(ValidationError):
        ProviderSettings(base_url="http://example.com/inference")
    assert ProviderSettings(base_url="http://127.0.0.1:9999/v1").base_url.endswith("/v1")
    with pytest.raises(ValidationError):
        ProviderSettings(api_key_env="not a name")


def test_schema_v1_config_loads_and_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": "openrouter/anthropic/claude-sonnet-5.5",
                "opencode": {"standalone": False},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert loaded.schema_version == 2
    assert loaded.provider.name == "opencode"
    assert loaded.model == "openrouter/anthropic/claude-sonnet-5.5"
    assert loaded.opencode.standalone is False
