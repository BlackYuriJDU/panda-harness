"""Panda model catalogue snapshot (offline fallback for the model picker).

The live source is `GET {base_url}/v1/models` on OpenCode Console. This snapshot
is only used when the network is unavailable, so `panda models` and `/models`
still explain what exists and which protocols are supported. Refresh it with
`panda models --refresh` and a new capture when the upstream catalogue changes.
"""

from __future__ import annotations

from panda_core.provider import ModelInfo

CAPTURED_AT = "2026-10-25"
SOURCE = "https://opencode.ai/inference/v1/models"

MODEL_IDS: tuple[str, ...] = (
    "big-pickle",
    "beam-free",
    "flint-40b-free",
    "mimo-v2.6-flash-free",
    "nemotron-3.5-super-vl-free",
    "nemotron-3.5-lightning-free",
    "jev-1.13-free",
    "deepseek-v4.1-flash-free",
    "glm-5.3-flash-free",
    "test",
    "claude-fable-5",
    "jev-1.13",
    "claude-fable-5-1",
    "claude-haiku-4-5",
    "claude-haiku-5-5",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-opus-5-5",
    "claude-opus-5-5-backup",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "claude-sonnet-5-5",
    "deepseek-v4-flash",
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "gemini-3-flash",
    "gemini-3.1-pro",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-4-argon",
    "glm-5",
    "glm-5.1",
    "glm-5.2",
    "glm-5.3",
    "glm-5.3-flash",
    "gpt-5",
    "gpt-5-codex",
    "gpt-5-nano",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.4-pro",
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6.1-sol",
    "gpt-6-luna",
    "gpt-5.6-terra",
    "grok-4.5",
    "grok-4.6",
    "grok-4.7",
    "grok-build-0.1",
    "kimi-k2.5",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "kimi-k3",
    "minimax-m2.5",
    "minimax-m2.7",
    "minimax-m3",
    "muse-spark-1.2",
    "muse-spark-1.3",
    "muse-wonder-1.0",
    "mistral-large-4",
    "qwen3.5-plus",
    "qwen3.6-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
)


def snapshot_models() -> list[ModelInfo]:
    return sorted(
        (ModelInfo.from_id(model_id) for model_id in MODEL_IDS),
        key=lambda model: model.id,
    )


__all__ = ["CAPTURED_AT", "MODEL_IDS", "SOURCE", "snapshot_models"]