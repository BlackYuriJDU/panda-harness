"""PLE provider profile built from the evaluator's environment."""

from __future__ import annotations

import pytest

from panda_core.errors import ConfigError
from panda_core.profiles import ple_provider_settings
from panda_core.provider import OpenCodeProvider

ENV = {
    "MODEL_BASE_URL": "https://gateway.example/v1",
    "MODEL_API_KEY": "secret-value",
    "MODEL_NAME": "claude-opus-5-5",
    "ANTHROPIC_BASE_URL": "https://gateway.example",
    "GOOGLE_GEMINI_BASE_URL": "https://gemini.example",
}


def test_ple_settings_for_opus() -> None:
    settings, model = ple_provider_settings(ENV)
    assert model == "google-cloud/claude-opus-5-5"
    assert settings.name == "google-cloud"
    assert settings.endpoint_profile == "generic"
    assert settings.api_key_env == "MODEL_API_KEY"
    assert settings.base_url == "https://gateway.example/v1"
    assert settings.anthropic_base_url == "https://gateway.example"
    assert settings.google_base_url == "https://gemini.example"
    assert settings.max_output_tokens == 32_000
    assert settings.max_retries == 5
    assert settings.max_retry_delay_seconds == 120
    assert settings.prompt_cache and settings.retry_partial
    assert settings.effort == "high"
    # The key is read from the environment by the provider, never copied.
    assert "secret-value" not in settings.model_dump_json()


def test_ple_settings_drive_the_provider() -> None:
    settings, model = ple_provider_settings(ENV)
    provider = OpenCodeProvider(settings, model=model, api_key="k")
    assert provider.model_id() == "claude-opus-5-5"
    assert provider.anthropic_url == "https://gateway.example/v1/messages"
    assert provider.chat_url == "https://gateway.example/v1/chat/completions"


def test_ple_bases_default_to_the_gateway_root() -> None:
    env = {k: v for k, v in ENV.items() if not k.endswith("_BASE_URL") or k == "MODEL_BASE_URL"}
    settings, _ = ple_provider_settings(env)
    assert settings.anthropic_base_url == "https://gateway.example"
    assert settings.google_base_url == "https://gateway.example"


def test_ple_model_prefix_is_not_doubled() -> None:
    _, model = ple_provider_settings({**ENV, "MODEL_NAME": "google-cloud/glm-5.2"})
    assert model == "google-cloud/glm-5.2"


@pytest.mark.parametrize(
    ("name", "effort"),
    [("gemini-4-argon", "high"), ("gpt-oss-120b", "high"), ("llama-4-maverick", None)],
)
def test_effort_only_for_reasoning_families(name: str, effort: str | None) -> None:
    settings, _ = ple_provider_settings({**ENV, "MODEL_NAME": name})
    assert settings.effort == effort


def test_missing_variables_fail_closed() -> None:
    with pytest.raises(ConfigError) as info:
        ple_provider_settings({"MODEL_BASE_URL": "https://gateway.example/v1"})
    assert info.value.details["missing"] == ["MODEL_NAME", "MODEL_API_KEY"]


def test_invalid_url_fails_closed() -> None:
    with pytest.raises(ConfigError):
        ple_provider_settings({**ENV, "MODEL_BASE_URL": "http://gateway.example/v1"})
