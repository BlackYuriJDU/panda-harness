"""Named provider profiles built from the process environment.

`ple_provider_settings` reads the variables the PLE evaluator injects into a
harness container and returns settings for the generic endpoint profile. It
never reads or returns the API key: the provider reads `MODEL_API_KEY` itself
when it builds the Authorization header.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from panda_core.config import DEFAULT_CONTEXT_WINDOW, ENDPOINT_GENERIC, Effort, ProviderSettings
from panda_core.errors import ConfigError

PLE_PROVIDER = "google-cloud"
PLE_API_KEY_ENV = "MODEL_API_KEY"
PLE_REQUIRED_ENV = ("MODEL_BASE_URL", "MODEL_NAME", PLE_API_KEY_ENV)


def _context_window(model_name: str) -> int:
    name = model_name.lower()
    if name.startswith(("claude", "gemini")):
        return 1_000_000
    return DEFAULT_CONTEXT_WINDOW


def _accepts_effort(model_name: str) -> bool:
    # Families documented to accept a reasoning effort; others may answer 400.
    return model_name.lower().startswith(("claude", "gemini", "gpt-oss", "glm"))


def ple_provider_settings(
    environ: Mapping[str, str] | None = None,
    *,
    effort: Effort | None = "high",
) -> tuple[ProviderSettings, str]:
    """Return `(settings, model_ref)` for the PLE model gateway.

    `MODEL_BASE_URL` already ends in `/v1`; `ANTHROPIC_BASE_URL` and
    `GOOGLE_GEMINI_BASE_URL` do not, and default to the host of
    `MODEL_BASE_URL` when absent. `effort` is sent only to model families
    that accept a reasoning effort.
    """

    env = os.environ if environ is None else environ
    missing = [name for name in PLE_REQUIRED_ENV if not env.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "PLE profile needs " + ", ".join(missing),
            hint="the evaluator injects these variables into the harness container",
            details={"missing": missing},
        )
    base_url = env["MODEL_BASE_URL"].strip().rstrip("/")
    root = base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url
    model_name = env["MODEL_NAME"].strip()
    prefix = f"{PLE_PROVIDER}/"
    if model_name.startswith(prefix):
        model_name = model_name[len(prefix) :]
    try:
        settings = ProviderSettings(
            name=PLE_PROVIDER,
            base_url=base_url,
            api_key_env=PLE_API_KEY_ENV,
            endpoint_profile=ENDPOINT_GENERIC,
            anthropic_base_url=env.get("ANTHROPIC_BASE_URL", "").strip() or root,
            google_base_url=env.get("GOOGLE_GEMINI_BASE_URL", "").strip() or root,
            # Max effort can think silently for minutes; the egress proxy closes
            # idle connections at 1800 s.
            timeout_seconds=1800.0,
            context_window=_context_window(model_name),
            max_output_tokens=32_000,
            max_retries=5,
            retry_base_seconds=5.0,
            max_retry_delay_seconds=120.0,
            effort=effort if _accepts_effort(model_name) else None,
            prompt_cache=True,
            retry_partial=True,
        )
    except ValueError as exc:  # pydantic ValidationError subclasses ValueError
        raise ConfigError(f"invalid PLE provider environment: {exc}") from exc
    return settings, f"{PLE_PROVIDER}/{model_name}"


__all__ = [
    "PLE_API_KEY_ENV",
    "PLE_PROVIDER",
    "PLE_REQUIRED_ENV",
    "ple_provider_settings",
]
