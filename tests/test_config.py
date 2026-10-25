"""Configuration: typed defaults, path safety, and deterministic JSON."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from panda_core.config import (
    DEFAULT_MODEL,
    OpencodeSettings,
    PandaConfig,
    load_config,
)
from panda_core.errors import ConfigError


def test_default_config_is_valid() -> None:
    config = PandaConfig()
    assert config.model == DEFAULT_MODEL
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


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PandaConfig.model_validate({"model": DEFAULT_MODEL, "unexpected": True})


def test_opencode_config_path_must_stay_inside_workspace() -> None:
    with pytest.raises(ValidationError):
        OpencodeSettings(config_path="../opencode.json")
    assert OpencodeSettings(config_path=".opencode/opencode.json").config_path == (
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
