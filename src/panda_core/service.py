"""Shared service layer used by the CLI, MCP server, and tests.

Keeping the policy/validation/provenance behavior here (rather than in the MCP
adapter) means the same rules apply regardless of the client.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from panda_core.errors import PandaError
from panda_core.opencode import config_status
from panda_core.policy import PolicyEngine, load_policy_or_default
from panda_core.registry import find_study, load_registry, load_study_card
from panda_core.session import SessionLog, list_sessions
from panda_core.validator import Validator
from panda_core.workspace import Workspace


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
            except (PandaError, OSError):
                self.session = None
                return False
        if self.session is None:
            return False
        try:
            self.session.append(event_type, data)
            return True
        except (PandaError, OSError):
            # Read-only tools remain usable if provenance is unavailable; the
            # tool result tells the client that it was not logged.
            return False

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
        info["opencode"] = config_status(self.workspace)
        info["instructions"] = {
            "path": str(self.workspace.instructions_path),
            "exists": self.workspace.instructions_path.is_file(),
        }
        info["sessions"] = len(list_sessions(self.workspace.sessions_dir))
        info["session_id"] = self.session_id
        return info

    def validate(self, *, release: bool = False) -> dict[str, Any]:
        logged = self._record("tool.call", {"tool": "validate", "release": release})
        validator = Validator(
            self.workspace.root,
            script=self.workspace.validator_path,
            node=self.workspace.config.node,
            min_major=self.workspace.config.node_min_major,
            timeout=self.workspace.config.timeouts.validate_seconds,
        )
        report = validator.run(release=release)
        payload = report.to_dict()
        payload["session_logged"] = logged
        self._record(
            "validate.result",
            {
                "ok": report.ok,
                "release_mode": report.release_mode,
                "passed": report.passed,
                "failed": report.failed,
                "skipped": report.skipped,
                "failed_checks": [
                    check.name
                    for check in report.checks
                    if not check.ok and not check.skipped
                ],
            },
        )
        return payload

    def policy_check(
        self, action: str, resource: str, *, approved: bool = False
    ) -> dict[str, Any]:
        policy, from_file = load_policy_or_default(self.workspace.policy_path)
        engine = PolicyEngine(policy, root=self.workspace.root)
        normalized = resource
        if action in {"read", "write", "edit", "delete"} and resource:
            normalized = self.workspace.relative_resource(resource)
        decision = engine.decide(action, normalized)
        if approved and decision.effect == "ask":
            decision = engine.authorize(action, normalized, approved=True)
        payload = decision.to_dict()
        payload["policy_source"] = "file" if from_file else "built-in default"
        payload["approved"] = approved
        self._record(
            "policy.decision",
            {
                "action": action,
                "resource": normalized,
                "effect": decision.effect,
                "rule_id": decision.rule_id,
                "risk": decision.risk,
                "approved": approved,
            },
        )
        return payload

    def study_list(
        self, *, track: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        studies = [study.to_dict() for study in load_registry(self.workspace.root)]
        if track:
            studies = [study for study in studies if study["track"] == track]
        if status:
            studies = [study for study in studies if study["status"] == status]
        self._record(
            "tool.call",
            {"tool": "study_list", "track": track, "status": status, "count": len(studies)},
        )
        return studies

    def study_show(self, study_id: str) -> dict[str, Any]:
        study = find_study(self.workspace.root, study_id)
        card = load_study_card(study)
        payload = study.to_dict()
        payload["frontmatter"] = card["frontmatter"]
        payload["body"] = card["body"][:8000]
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
