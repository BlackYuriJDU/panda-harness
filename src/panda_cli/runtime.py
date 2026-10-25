"""Shared chat runtime for the cockpit, the plain REPL, and headless `panda run`.

One runtime owns one workspace, one provider, one policy engine, one tool
registry, one session ledger, and one agent. The UI layer only collects input,
renders events, and answers approvals; enforcement stays below it.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from panda_core.agent import Agent, AgentEvent, TurnResult
from panda_core.config import split_model
from panda_core.errors import ConfigError
from panda_core.model_catalog import CAPTURED_AT, snapshot_models
from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.provider import (
    ModelInfo,
    OpenCodeProvider,
    classify_protocol,
    status_snapshot,
)
from panda_core.session import SessionLog
from panda_core.tools import ApprovalRequest, ToolContext, ToolRegistry, ToolResult
from panda_core.version import __version__
from panda_core.workspace import Workspace

ApprovalCallback = Callable[[ApprovalRequest], Awaitable[bool]]
EventCallback = Callable[[AgentEvent], None]


def _env_names() -> list[str]:
    return sorted(
        name
        for name in os.environ
        if name.startswith(("PANDA_", "OPENCODE_"))
    )


class ChatRuntime:
    def __init__(
        self,
        workspace: Workspace,
        *,
        model: str | None = None,
        max_steps: int = 24,
        private: bool = False,
        auto: bool = False,
        active_study: str | None = None,
        session: SessionLog | None = None,
        approver: ApprovalCallback | None = None,
        on_event: EventCallback | None = None,
        label: str = "chat",
    ) -> None:
        self.workspace = workspace
        self.private = private
        self.auto = auto
        self._approver = approver
        self._on_event = on_event
        self._model_source = "config"
        policy, _ = load_policy_or_default(workspace.policy_path)
        self.policy = PolicyEngine(policy, root=workspace.root)
        if session is None:
            workspace.ensure_runtime_dirs()
            session = SessionLog.create(
                workspace.sessions_dir,
                label=label,
                metadata={
                    "cwd": str(Path.cwd()),
                    "panda_version": __version__,
                    "git": workspace.git_state(),
                    "provider": workspace.config.provider.name,
                    "base_url": workspace.config.provider.base_url,
                    "env_names": _env_names(),
                    "transport": "cockpit",
                },
                private=private,
                redact_enabled=workspace.config.logging.redact,
            )
        self.session = session
        self.provider = OpenCodeProvider(
            workspace.config.provider, model=model or workspace.config.model
        )
        self.ctx = ToolContext(
            workspace=workspace,
            policy=self.policy,
            session=session,
            active_study=active_study,
            approve=None,
        )
        self.tools = ToolRegistry(self.ctx)
        self.agent = Agent(
            workspace,
            self.provider,
            self.tools,
            session=session,
            model=model or workspace.config.model,
            max_steps=max_steps,
            active_study=active_study,
            approver=self._approve,
            on_event=on_event,
            private=private,
        )
        self.last_validation: dict[str, Any] | None = None
        self._models: list[ModelInfo] | None = None
        self._models_source = "none"
        self._models_error: str | None = None

    # ---- approvals ----
    async def _approve(self, request: ApprovalRequest) -> bool:
        if self.auto:
            return True
        if self._approver is not None:
            return bool(await self._approver(request))
        return False

    # ---- conversation ----
    async def ask(self, message: str) -> TurnResult:
        return await self.agent.run_turn(message)

    async def validate(self, *, release: bool = False) -> ToolResult:
        result = await self.tools.call("validate", {"release": release})
        self.last_validation = result.data
        return result

    def set_active_study(self, study_id: str | None) -> None:
        self.agent.set_active_study(study_id)

    @property
    def active_study(self) -> str | None:
        return self.agent.active_study

    @property
    def model(self) -> str:
        return self.agent.model

    def set_model(self, model: str) -> None:
        provider, name = split_model(model)
        if not name:
            raise ConfigError("model must use the provider/model form")
        if provider != self.workspace.config.provider.name:
            raise ConfigError(
                f"model provider {provider!r} is not configured",
                hint=f"use `panda models`; the provider is {self.workspace.config.provider.name}",
            )
        protocol = classify_protocol(name)
        if protocol != "openai-chat":
            raise ConfigError(
                f"model {model!r} uses {protocol}, which is not implemented",
                hint="pick an OpenAI-compatible model; `panda models` marks the rest unsupported",
            )
        self.agent.set_model(model)
        self.provider.model = model
        self._model_source = "manual"

    def reset(self) -> None:
        self.agent.reset_conversation()

    # ---- models ----
    async def models(
        self, *, refresh: bool = False, allow_snapshot: bool = True
    ) -> list[ModelInfo]:
        if self._models is not None and not refresh:
            return self._models
        try:
            self._models = await self.provider.list_models()
            self._models_source = "live"
            self._models_error = None
        except Exception as exc:  # noqa: BLE001 - offline fallback is deliberate
            self._models_error = getattr(exc, "message", str(exc))
            if not allow_snapshot:
                raise
            self._models = snapshot_models()
            self._models_source = "snapshot"
        return self._models

    @property
    def models_source(self) -> str:
        return self._models_source

    @property
    def models_error(self) -> str | None:
        return self._models_error

    @property
    def snapshot_captured_at(self) -> str:
        return CAPTURED_AT
    # ---- status ----
    def status(self) -> dict[str, Any]:
        git = self.workspace.git_state()
        snapshot = status_snapshot(self.provider)
        usage = self.agent.usage_total
        return {
            "root": str(self.workspace.root),
            "initialized": self.workspace.initialized,
            "branch": git.get("branch"),
            "dirty": git.get("dirty"),
            "commit": git.get("commit"),
            "provider": snapshot["name"],
            "base_url": snapshot["base_url"],
            "api_key_env": snapshot["api_key_env"],
            "api_key_present": snapshot["api_key_present"],
            "model": self.model,
            "active_study": self.active_study,
            "validator": self.workspace.config.validator,
            "validator_exists": self.workspace.validator_path.is_file(),
            "validation": self.last_validation,
            "session_id": self.session.session_id,
            "session_closed": self.session.is_closed(),
            "turn": self.agent.turn_index,
            "max_steps": self.agent.max_steps,
            "last_steps": self.agent.last_steps,
            "context_tokens": self.agent.last_context_tokens,
            "context_window": self.agent.context_window,
            "context_ratio": round(self.agent.context_ratio, 4),
            "usage": usage.to_dict(),
            "last_usage": self.agent.last_usage.to_dict(),
            "models_source": self._models_source,
        }

    def policy_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "id": rule.id,
                "effect": rule.effect,
                "risk": rule.risk,
                "actions": ", ".join(rule.actions),
            }
            for rule in self.policy.policy.rules
        ]

    def session_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "seq": event.seq,
                "ts": event.ts,
                "type": event.type,
            }
            for event in self.session.events()
        ]

    def close(self, status: str = "completed") -> None:
        import contextlib

        if self.session.is_closed():
            return
        with contextlib.suppress(Exception):
            self.session.close(status, {"model": self.model, "turns": self.agent.turn_index})

    async def aclose(self) -> None:
        await self.provider.aclose()
        self.close()


__all__ = ["ApprovalCallback", "ChatRuntime", "EventCallback"]