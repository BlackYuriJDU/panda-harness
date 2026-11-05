"""Shared service layer used by the CLI, MCP server, and tests.

Keeping the policy/validation/provenance behavior here (rather than in the MCP
adapter) means the same rules apply regardless of the client.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from panda_core import errors
from panda_core.errors import PandaError, PolicyDenied, SessionIntegrityError
from panda_core.policy import engine_for, load_policy_or_default
from panda_core.provider import status_snapshot
from panda_core.session import SessionLog, list_sessions
from panda_core.tools import ToolContext, ToolRegistry, ToolResult
from panda_core.workspace import Workspace

_ERROR_CLASSES: dict[str, type[PandaError]] = {
    errors.ConfigError.code: errors.ConfigError,
    errors.DependencyError.code: errors.DependencyError,
    errors.ProtocolError.code: errors.ProtocolError,
    errors.ExecutionTimeout.code: errors.ExecutionTimeout,
    errors.UsageError.code: errors.UsageError,
}


def _run(coro: Coroutine[Any, Any, ToolResult]) -> ToolResult:
    """Run a coroutine from sync code, also when an event loop is already running."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class PandaService:
    def __init__(
        self,
        workspace: Workspace,
        *,
        session: SessionLog | None = None,
        autocreate_session: bool = False,
        session_label: str = "mcp",
    ) -> None:
        self.workspace = workspace
        self.session = session
        self.autocreate_session = autocreate_session
        self.session_label = session_label
        self.owns_session = False

    @classmethod
    def from_env(cls, root: str | Path | None = None) -> PandaService:
        explicit = root or os.environ.get("PANDA_ROOT")
        workspace = Workspace.load(Path(explicit)) if explicit else Workspace.load()
        session_file = os.environ.get("PANDA_SESSION_FILE")
        session = None
        autocreate = True
        if session_file:
            path = Path(session_file)
            if path.is_file():
                session = SessionLog.open(
                    path, redact_enabled=workspace.config.logging.redact
                )
                autocreate = False
        return cls(workspace, session=session, autocreate_session=autocreate)

    # ---- provenance ----
    def _record(self, event_type: str, data: dict[str, Any]) -> bool:
        if self.session is None and self.autocreate_session:
            try:
                self.session = SessionLog.create(
                    self.workspace.sessions_dir,
                    label=self.session_label,
                    metadata={
                        "root": str(self.workspace.root),
                        "panda_version": self.workspace.config.created_with,
                        "transport": "mcp",
                    },
                    redact_enabled=self.workspace.config.logging.redact,
                )
                self.owns_session = True
                self.session.append(
                    "mcp.connect",
                    {"cwd": str(Path.cwd()), "pid": os.getpid()},
                )
            except (PandaError, OSError) as exc:
                self.session = None
                raise SessionIntegrityError(
                    f"cannot create the MCP session ledger: {exc}",
                    hint="free disk space or fix .panda/sessions; the tool did not run",
                ) from exc
        if self.session is None:
            return False
        try:
            self.session.append(event_type, data)
            return True
        except SessionIntegrityError:
            raise
        except (PandaError, OSError) as exc:
            raise SessionIntegrityError(
                f"cannot write the session ledger ({event_type}): {exc}",
                hint="run `panda session verify`; the result was not returned",
            ) from exc

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a tool through the same registry and policy the agent uses (MCP-002).

        MCP has no approver, so an `ask` is denied exactly as in `panda run`
        without `--auto`. A denial is recorded and raised; a tool error is raised
        with its original class.
        """

        policy, from_file = load_policy_or_default(self.workspace.policy_path)
        registry = ToolRegistry(
            ToolContext(
                workspace=self.workspace,
                policy=engine_for(self.workspace, policy, from_file=from_file),
                session=self.session,
                approve=None,
            )
        )
        result = _run(registry.call(name, arguments))
        data = dict(result.data)
        if result.blocked:
            keys = ("tool", "action", "resource", "effect", "rule", "reason")
            self._record("policy.decision", {key: data.get(key) for key in keys})
            raise PolicyDenied(
                f"{name} denied by policy", hint=str(data.get("reason") or ""), details=data
            )
        if "error" in data:
            error_class = _ERROR_CLASSES.get(str(data.get("code")), PandaError)
            raise error_class(str(data["error"]))
        return data

    @property
    def session_id(self) -> str | None:
        return self.session.session_id if self.session else None

    def close(self) -> None:
        if self.session and self.owns_session and not self.session.is_closed():
            with contextlib.suppress(PandaError, OSError):
                self.session.close("mcp_closed")

    # ---- tools ----
    def workspace_info(self) -> dict[str, Any]:
        info = self.workspace.describe()
        policy, policy_from_file = load_policy_or_default(self.workspace.policy_path)
        info["policy"] = {
            "path": str(self.workspace.policy_path),
            "source": "file" if policy_from_file else "built-in default",
            "rules": len(policy.rules),
        }
        info["provider"] = status_snapshot()
        info["provider"]["model"] = self.workspace.config.model
        info["instructions"] = {
            "path": str(self.workspace.instructions_path),
            "exists": self.workspace.instructions_path.is_file(),
        }
        info["sessions"] = len(list_sessions(self.workspace.sessions_dir))
        info["session_id"] = self.session_id
        return info

    def validate(self, *, release: bool = False) -> dict[str, Any]:
        logged = self._record("tool.call", {"tool": "validate", "release": release})
        payload = self._call_tool("validate", {"release": release})
        payload.pop("tool", None)
        payload["session_logged"] = logged
        summary = payload["summary"]
        self._record(
            "validate.result",
            {
                "ok": payload["ok"],
                "release_mode": payload["release_mode"],
                "passed": summary["passed"],
                "failed": summary["failed"],
                "skipped": summary["skipped"],
                "failed_checks": [
                    check["name"]
                    for check in payload["checks"]
                    if not check["ok"] and not check["skipped"]
                ],
                "validator_sha256": payload.get("validator_sha256"),
            },
        )
        return payload

    def policy_check(
        self, action: str, resource: str, *, approved: bool = False
    ) -> dict[str, Any]:
        policy, from_file = load_policy_or_default(self.workspace.policy_path)
        engine = engine_for(self.workspace, policy, from_file=from_file)
        normalized = resource
        if action in {"read", "write", "edit", "delete"} and resource:
            normalized = self.workspace.relative_resource(resource)
        decision = engine.decide(action, normalized)
        # `approved` is deprecated and ignored: an MCP client is not an approver, and
        # it used to turn an `ask` (even `git push`) into a logged allow (R2-406).
        payload = decision.to_dict()
        payload["policy_source"] = "file" if from_file else "built-in default"
        payload["approved"] = False
        if approved:
            payload["deprecated"] = "approved is ignored; approval needs a person"
        self._record(
            "policy.decision",
            {
                "action": action,
                "resource": normalized,
                "effect": decision.effect,
                "rule_id": decision.rule_id,
                "risk": decision.risk,
                "approved": False,
                "approved_requested": approved,
            },
        )
        return payload

    def study_list(
        self, *, track: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        # Through the registry, like the agent: the read policy on INDEX.md applies
        # and the call is recorded by the same path (R2-118, MCP-002 parity).
        arguments: dict[str, Any] = {}
        if track:
            arguments["track"] = track
        if status:
            arguments["status"] = status
        data = self._call_tool("study_list", arguments)
        return list(data.get("studies", []))

    def study_show(self, study_id: str) -> dict[str, Any]:
        data = self._call_tool("study_show", {"study_id": study_id})
        payload = {key: value for key, value in data.items() if key not in {"ok", "tool"}}
        self._record("tool.call", {"tool": "study_show", "study_id": study_id})
        return payload

    def note(self, message: str) -> dict[str, Any]:
        text = message.strip()
        if not text:
            raise ValueError("note message is empty")
        if len(text) > 4000:
            text = text[:4000] + "\n[truncated]"
        logged = self._record("note", {"message": text})
        return {
            "ok": logged,
            "session_id": self.session_id,
            "recorded": logged,
            "message_bytes": len(text.encode("utf-8")),
        }
