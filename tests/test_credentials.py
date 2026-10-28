"""The user-scoped credential store: precedence, atomicity, and no leakage."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from panda_core import credentials
from panda_core.config import ProviderSettings
from panda_core.errors import ConfigError, ProviderError, UsageError
from panda_core.provider import OpenCodeProvider

MODEL = "opencode-go/deepseek-v4.1-flash"


def _provider() -> OpenCodeProvider:
    return OpenCodeProvider(ProviderSettings(), model=MODEL)


def test_path_honors_the_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "creds.json"
    monkeypatch.setenv(credentials.CREDENTIALS_ENV, str(target))
    assert credentials.credentials_path() == target


def test_roundtrip_writes_a_private_file(isolated_credentials: Path) -> None:
    assert credentials.get_api_key("opencode-go") is None

    path = credentials.set_api_key("opencode-go", "  oc_secret_value  ")

    assert path == isolated_credentials
    assert credentials.get_api_key("opencode-go") == "oc_secret_value"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "keys": {"opencode-go": "oc_secret_value"}}
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600

    assert credentials.clear_api_key("opencode-go") is True
    assert credentials.get_api_key("opencode-go") is None
    assert not path.exists()
    assert credentials.clear_api_key("opencode-go") is False


def test_multiple_providers_are_independent(isolated_credentials: Path) -> None:
    credentials.set_api_key("opencode-go", "go-key")
    credentials.set_api_key("other-provider", "other-key")

    assert credentials.get_api_key("opencode-go") == "go-key"
    assert credentials.get_api_key("other-provider") == "other-key"

    assert credentials.clear_api_key("opencode-go") is True
    assert credentials.get_api_key("other-provider") == "other-key"


def test_clear_all_removes_the_file(isolated_credentials: Path) -> None:
    credentials.set_api_key("opencode-go", "go-key")
    assert credentials.clear_all() is True
    assert not isolated_credentials.exists()
    assert credentials.clear_all() is False


def test_rejects_invalid_provider_and_key(isolated_credentials: Path) -> None:
    with pytest.raises(UsageError):
        credentials.set_api_key("Not A Provider", "value")
    with pytest.raises(UsageError):
        credentials.set_api_key("opencode-go", "   ")
    with pytest.raises(UsageError):
        credentials.set_api_key("opencode-go", "two words")


def test_corrupt_store_raises_a_config_error(isolated_credentials: Path) -> None:
    isolated_credentials.parent.mkdir(parents=True, exist_ok=True)
    isolated_credentials.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError):
        credentials.get_api_key("opencode-go")


def test_mask_never_returns_the_full_key() -> None:
    assert credentials.mask_key(None) == "not set"
    masked = credentials.mask_key("oc_secret_value")
    assert "oc_secret_value" not in masked
    assert masked.endswith("alue)")


def test_environment_wins_over_the_store(
    isolated_credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials.set_api_key("opencode-go", "stored-key")
    provider = _provider()

    monkeypatch.setenv("OPENCODE_API_KEY", "env-key")
    assert provider.api_key() == "env-key"

    monkeypatch.delenv("OPENCODE_API_KEY")
    assert provider.api_key() == "stored-key"


def test_provider_reads_the_store_when_the_env_is_missing(
    isolated_credentials: Path,
) -> None:
    provider = _provider()
    assert provider.has_api_key() is False

    credentials.set_api_key("opencode-go", "stored-key")

    assert provider.has_api_key() is True
    assert provider.api_key() == "stored-key"


def test_provider_error_names_both_sources(isolated_credentials: Path) -> None:
    provider = _provider()
    with pytest.raises(ProviderError) as raised:
        provider._require_api_key()
    assert "OPENCODE_API_KEY" in raised.value.message
    assert "panda auth set" in raised.value.hint
