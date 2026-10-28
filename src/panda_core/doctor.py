"""`panda doctor`: diagnose the workspace, runtime, and fail-closed surfaces."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any

from panda_core import credentials
from panda_core.config import split_model
from panda_core.errors import PandaError
from panda_core.policy import load_policy
from panda_core.protocols import SUPPORTED_PROTOCOLS, classify_protocol
from panda_core.validator import Validator
from panda_core.version import __version__
from panda_core.workspace import Workspace


@dataclass
class DoctorCheck:
    id: str
    label: str
    status: str  # ok | warn | fail
    detail: str = ""
    hint: str = ""
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "required": self.required,
        }


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(check.required and check.status == "fail" for check in self.checks)

    @property
    def failures(self) -> int:
        return sum(1 for check in self.checks if check.status == "fail")

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if check.status == "warn")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
            "summary": {
                "ok": sum(1 for check in self.checks if check.status == "ok"),
                "warn": self.warnings,
                "fail": self.failures,
                "total": len(self.checks),
            },
        }


def _check(
    checks: list[DoctorCheck],
    check_id: str,
    label: str,
    status: str,
    detail: str = "",
    hint: str = "",
    required: bool = False,
) -> None:
    checks.append(
        DoctorCheck(
            id=check_id,
            label=label,
            status=status,
            detail=detail,
            hint=hint,
            required=required,
        )
    )


def run_doctor(workspace: Workspace, *, run_validation: bool = False) -> DoctorReport:
    checks: list[DoctorCheck] = []

    py_ok = sys.version_info >= (3, 12)
    _check(
        checks,
        "python",
        "Python 3.12+",
        "ok" if py_ok else "fail",
        sys.version.split()[0],
        "install Python 3.12 or newer",
        required=True,
    )
    _check(checks, "panda", "Panda harness", "ok", __version__, required=False)

    _check(
        checks,
        "workspace",
        "Workspace initialized",
        "ok" if workspace.initialized else "warn",
        str(workspace.root),
        "run `panda init` to write .panda/config.json",
        required=False,
    )

    _check(
        checks,
        "validator",
        "Corpus validator",
        "ok" if workspace.validator_path.is_file() else "fail",
        str(workspace.validator_path),
        "a corpus root contains corpus/validation/validate.mjs; pass --validator otherwise",
        required=True,
    )

    try:
        node_version = Validator(
            workspace.root,
            script=workspace.validator_path,
            node=workspace.config.node,
            min_major=workspace.config.node_min_major,
        ).node_version()
        _check(
            checks,
            "node",
            f"Node.js {workspace.config.node_min_major}+",
            "ok",
            node_version,
            required=True,
        )
    except PandaError as exc:
        _check(
            checks,
            "node",
            f"Node.js {workspace.config.node_min_major}+",
            "fail",
            exc.message,
            exc.hint or "install Node.js 20+ and ensure it is on PATH",
            required=True,
        )

    policy_status = "warn"
    policy_detail = str(workspace.policy_path)
    policy_hint = "run `panda init` to generate the default policy"
    if workspace.policy_path.is_file():
        try:
            policy = load_policy(workspace.policy_path)
            policy_status = "ok"
            policy_detail = f"{len(policy.rules)} rules"
        except PandaError as exc:
            policy_status = "fail"
            policy_detail = exc.message
            policy_hint = "fix .panda/policy.json or regenerate it with `panda init --force`"
    _check(
        checks,
        "policy",
        "Policy",
        policy_status,
        policy_detail,
        policy_hint,
        required=workspace.policy_path.is_file(),
    )

    sessions_dir = workspace.sessions_dir
    if sessions_dir.is_dir():
        try:
            with tempfile.NamedTemporaryFile(
                dir=sessions_dir, prefix=".doctor-", delete=True
            ) as handle:
                handle.write(b"ok")
                handle.flush()
            _check(
                checks,
                "sessions",
                "Session ledger writable",
                "ok",
                str(sessions_dir),
                required=True,
            )
        except OSError as exc:
            _check(
                checks,
                "sessions",
                "Session ledger writable",
                "fail",
                str(exc),
                "fix permissions on .panda/sessions",
                required=True,
            )
    else:
        existing_parent = sessions_dir.parent
        while not existing_parent.exists() and existing_parent != existing_parent.parent:
            existing_parent = existing_parent.parent
        writable = os.access(existing_parent, os.W_OK)
        _check(
            checks,
            "sessions",
            "Session ledger writable",
            "warn" if writable else "fail",
            f"not created yet; nearest parent writable: {writable}",
            "run `panda init` to create .panda/sessions",
            required=not writable,
        )

    mcp_present = importlib.util.find_spec("mcp") is not None
    _check(
        checks,
        "mcp",
        "MCP server dependency",
        "ok" if mcp_present else "warn",
        "installed" if mcp_present else "not installed",
        "install with `pip install 'panda-harness[mcp]'` to use `panda mcp serve`",
        required=False,
    )

    provider = workspace.config.provider
    _check(
        checks,
        "provider",
        "Model provider",
        "ok",
        f"{provider.name} · {provider.base_url}",
        required=True,
    )
    stored_key = credentials.get_api_key(provider.name)
    key_present = bool(os.environ.get(provider.api_key_env) or stored_key)
    key_source = (
        "environment"
        if os.environ.get(provider.api_key_env)
        else ("credential store" if stored_key else "not set")
    )
    _check(
        checks,
        "provider_key",
        provider.api_key_env,
        "ok" if key_present else "warn",
        key_source,
        f"run `panda auth set` (or export {provider.api_key_env}) before "
        "`panda run` or `panda`; Panda records the source name only",
        required=False,
    )
    _, model_name = split_model(workspace.config.model)
    protocol = classify_protocol(model_name or workspace.config.model)
    _check(
        checks,
        "model",
        f"Model ({protocol})",
        "ok" if protocol in SUPPORTED_PROTOCOLS else "warn",
        workspace.config.model,
        "openai-chat and anthropic (Claude) are implemented; "
        "pick a supported id with `panda models`",
        required=False,
    )
    if workspace.opencode_config_path.is_file():
        _check(
            checks,
            "opencode_legacy",
            "Legacy opencode.json",
            "warn",
            str(workspace.opencode_config_path),
            "present but unused by the Panda runtime; `panda init --opencode` maintains it",
            required=False,
        )

    git = workspace.git_state()
    if git.get("repo"):
        _check(
            checks,
            "git",
            "Git provenance",
            "ok",
            f"{str(git.get('commit') or '')[:12]} dirty={git.get('dirty')}",
        )
    else:
        _check(
            checks,
            "git",
            "Git provenance",
            "warn",
            "not a git repository",
            "initialize git to record commit provenance in sessions",
        )

    if run_validation:
        try:
            report = Validator(
                workspace.root,
                script=workspace.validator_path,
                node=workspace.config.node,
                min_major=workspace.config.node_min_major,
                timeout=workspace.config.timeouts.validate_seconds,
            ).run()
            _check(
                checks,
                "validation",
                "Corpus validation",
                "ok" if report.ok else "fail",
                f"{report.passed} passed, {report.failed} failed, {report.skipped} skipped",
                "run `panda validate` for the failing check details",
                required=True,
            )
        except PandaError as exc:
            _check(
                checks,
                "validation",
                "Corpus validation",
                "fail",
                exc.message,
                exc.hint or "run `panda validate` directly",
                required=True,
            )

    return DoctorReport(checks=checks)
