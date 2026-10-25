"""Panda model catalogue snapshot (offline fallback for the model picker).

The live source is `GET {base_url}/v1/models` on OpenCode Go. This snapshot is
only used when the network is unavailable, so `panda models` and `/models`
still explain what exists and which protocols are supported. Refresh it with
`panda models --refresh` and a new capture when the upstream catalogue changes.
"""

from __future__ import annotations

from panda_core.provider import ModelInfo

CAPTURED_AT = "2026-10-25"
SOURCE = "https://opencode.ai/inference/go/v1/models"

MODEL_IDS: tuple[str, ...] = (
    "beam-free",
    "claude-haiku-5-5",
    "deepseek-v4-pro",
    "deepseek-v4.1-flash",
    "gemini-4-argon",
    "glm-5.1",
    "glm-5.2",
    "glm-5.3",
    "glm-5.3-flash",
    "gpt-5.6-luna",
    "gpt-6-luna",
    "grok-4.6",
    "grok-4.7",
    "hy3",
    "hy4-preview",
    "iceman-contributor-free",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "kimi-k3",
    "kimi-k3.1-pro",
    "longcat-2.0",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "mimo-v2.6-flash",
    "mimo-v2.6-pro",
    "minimax-m2.5",
    "minimax-m2.7",
    "minimax-m3",
    "minimax-m3.1-flash",
    "muse-spark-1.2-contributor",
    "muse-spark-1.3-contributor",
    "omen-alpha",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
    "step-5-preview",
)


def snapshot_models() -> list[ModelInfo]:
    return sorted(
        (ModelInfo.from_id(model_id) for model_id in MODEL_IDS),
        key=lambda model: model.id,
    )


__all__ = ["CAPTURED_AT", "MODEL_IDS", "SOURCE", "snapshot_models"]
