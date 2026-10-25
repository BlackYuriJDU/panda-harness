"""Typed errors and stable process exit codes.

Exit codes are part of the public contract (docs/CONTRACT.md). Do not renumber
them without a contract revision.
"""

from __future__ import annotations

from typing import Any


class PandaError(Exception):
    """Base error with a stable code and process exit status."""

    code = "internal_error"
    exit_code = 6

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "exit_code": self.exit_code,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload


class UsageError(PandaError):
    code = "usage_error"
    exit_code = 2


class ConfigError(PandaError):
    code = "config_error"
    exit_code = 2


class DependencyError(PandaError):
    code = "dependency_missing"
    exit_code = 3


class ProtocolError(PandaError):
    code = "protocol_error"
    exit_code = 4


class ValidationFailed(PandaError):
    code = "validation_failed"
    exit_code = 1


class PolicyDenied(PandaError):
    code = "policy_denied"
    exit_code = 5


class ExecutionTimeout(PandaError):
    code = "execution_timeout"
    exit_code = 4


class SessionIntegrityError(PandaError):
    code = "session_integrity_error"
    exit_code = 4
