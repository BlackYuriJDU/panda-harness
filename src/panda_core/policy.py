"""Fail-closed policy engine.

Rules are evaluated in order and the last match wins. `hard_deny` rules are
non-overridable: they win even if a later rule would allow the action. This is
what makes secret access and destructive git operations impossible to approve
away. An `ask` decision outside an interactive prompt is a denial.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from panda_core.atomic import atomic_write_json
from panda_core.errors import ConfigError, PolicyDenied

Effect = Literal["allow", "deny", "ask", "hard_deny"]
Risk = Literal["low", "medium", "high", "critical"]

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PolicyRule:
    id: str
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    effect: Effect
    risk: Risk = "low"
    reason: str = ""

    def matches(self, action: str, resource: str) -> bool:
        action_l = action.lower()
        resource_l = resource.lower()
        action_ok = any(
            fnmatch.fnmatchcase(action_l, pattern.lower()) for pattern in self.actions
        )
        resource_ok = any(
            fnmatch.fnmatchcase(resource_l, pattern.lower()) for pattern in self.resources
        )
        return action_ok and resource_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "actions": list(self.actions),
            "resources": list(self.resources),
            "effect": self.effect,
            "risk": self.risk,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Policy:
    rules: tuple[PolicyRule, ...]
    default: Literal["allow", "deny", "ask"] = "ask"
    schema_version: int = _SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "default": self.default,
            "rules": [rule.to_dict() for rule in self.rules],
        }


@dataclass(frozen=True)
class Decision:
    action: str
    resource: str
    effect: Effect
    risk: Risk
    rule_id: str | None
    reason: str
    matched_rules: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"

    @property
    def requires_approval(self) -> bool:
        return self.effect == "ask"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "resource": self.resource,
            "effect": self.effect,
            "risk": self.risk,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "matched_rules": list(self.matched_rules),
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
        }


def _rule(
    rule_id: str,
    actions: list[str],
    resources: list[str],
    effect: Effect,
    risk: Risk = "low",
    reason: str = "",
) -> PolicyRule:
    return PolicyRule(
        id=rule_id,
        actions=tuple(actions),
        resources=tuple(resources),
        effect=effect,
        risk=risk,
        reason=reason,
    )


def default_policy() -> Policy:
    """The v0.1 policy envelope for a DCJ-like evidence corpus."""

    rules = (
        # Reads are open inside the workspace except for credential material.
        _rule("read-default", ["read"], ["*"], "allow"),
        _rule(
            "read-secrets",
            ["read"],
            ["*.env", "*.env.*", "*/.env", "*/.env.*", "*.pem", "*.key"],
            "hard_deny",
            "critical",
            "credential material",
        ),
        _rule(
            "read-private-keys",
            ["read"],
            ["*id_rsa*", "*id_ed25519*", "*/.ssh/*", "*/.aws/credentials"],
            "hard_deny",
            "critical",
            "private key or cloud credential",
        ),
        _rule(
            "read-agent-credentials",
            ["read"],
            ["*/.config/opencode/service.json", "*/.claude/.credentials.json"],
            "hard_deny",
            "critical",
            "agent credential store",
        ),
        # Writes are limited to the corpus and memory scopes. The broad deny is
        # first so specific allows later in the list win (last match wins).
        _rule("write-default-deny", ["write", "edit"], ["*"], "deny", "high"),
        _rule("write-scopes", ["write", "edit"], ["corpus/*", "memory/*"], "allow"),
        _rule(
            "write-release",
            ["write", "edit"],
            ["corpus/releases/*"],
            "ask",
            "high",
            "release manifests require review",
        ),
        _rule(
            "write-review-scopes",
            ["write", "edit"],
            ["preprint/*", "pipeline/*", "tests/*", "rules/*", "research/*", "colaboracao/*"],
            "ask",
            "high",
            "review-owned scope",
        ),
        _rule("delete-default-deny", ["delete"], ["*"], "deny", "high"),
        _rule(
            "delete-corpus",
            ["delete"],
            ["corpus/*", "memory/*"],
            "ask",
            "high",
            "deletion is irreversible",
        ),
        # Broad shell ask first; specific read-only allows later.
        _rule("shell-default-ask", ["shell"], ["*"], "ask", "medium"),
        _rule(
            "shell-readonly",
            ["shell"],
            [
                "git status*",
                "git diff*",
                "git log*",
                "git show*",
                "git branch*",
                "node corpus/validation/validate.mjs*",
                "panda validate*",
            ],
            "allow",
        ),
        _rule("shell-push", ["shell"], ["git push*"], "ask", "high"),
        # Non-overridable destructive operations.
        _rule(
            "shell-force-push",
            ["shell"],
            [
                "git push *--force*",
                "git push *-f*",
                "git push *--mirror*",
                "git push *--delete*",
            ],
            "hard_deny",
            "critical",
            "history rewrite or remote deletion",
        ),
        _rule(
            "shell-destructive",
            ["shell"],
            [
                "git reset *--hard*",
                "git clean *-f*",
                "rm -rf*",
                "rm -fr*",
                "Remove-Item *-Recurse*",
                "Remove-Item *-Force*",
                "format *",
                "diskpart*",
                "shutdown*",
                "*|*sh*",
                "*|*bash*",
            ],
            "hard_deny",
            "critical",
            "destructive or remote-code operation",
        ),
        # Network access is an explicit decision, never implicit.
        _rule("network-default-ask", ["network", "webfetch", "websearch"], ["*"], "ask", "medium"),
        _rule("git-default-ask", ["git"], ["*"], "ask", "medium"),
        _rule("git-readonly", ["git"], ["status*", "diff*", "log*", "show*", "branch*"], "allow"),
        _rule("git-push", ["git"], ["push*"], "ask", "high"),
        _rule(
            "git-force-push",
            ["git"],
            ["push*--force*", "push*-f*", "push*--mirror*"],
            "hard_deny",
            "critical",
            "history rewrite",
        ),
        _rule("release-build", ["release"], ["build*"], "allow"),
        _rule(
            "release-publish",
            ["release"],
            ["publish*"],
            "hard_deny",
            "critical",
            "v0.1 never publishes; use the release workflow with human approval",
        ),
        _rule("spend-default-ask", ["spend"], ["*"], "ask", "critical"),
        # MCP tool calls. The server enforces path-level policy for its tools.
        _rule("tool-default-ask", ["tool"], ["*"], "ask", "medium"),
        _rule(
            "tool-panda-readonly",
            ["tool"],
            [
                "panda_workspace_info",
                "panda_validate",
                "panda_policy_check",
                "panda_study_list",
                "panda_study_show",
                "panda_session_note",
            ],
            "allow",
        ),
        _rule("question-allow", ["question"], ["*"], "allow"),
        # Anything not named above is an explicit decision.
        _rule("external-default-ask", ["external_read", "external_write"], ["*"], "ask", "high"),
    )
    return Policy(rules=rules, default="ask")


def load_policy(path: Path) -> Policy:
    if not path.is_file():
        raise ConfigError(
            f"policy not found: {path}",
            hint="run `panda init` to generate the default policy",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid policy JSON in {path}: {exc.msg}") from exc
    try:
        version = int(raw.get("schema_version", 0))
        if version != _SCHEMA_VERSION:
            raise ConfigError(f"unsupported policy schema_version: {version}")
        default = raw.get("default", "ask")
        if default not in {"allow", "deny", "ask"}:
            raise ConfigError(f"invalid policy default: {default}")
        rules: list[PolicyRule] = []
        for index, item in enumerate(raw.get("rules", [])):
            rules.append(
                PolicyRule(
                    id=str(item["id"]),
                    actions=tuple(str(value) for value in item["actions"]),
                    resources=tuple(str(value) for value in item["resources"]),
                    effect=item["effect"],
                    risk=item.get("risk", "low"),
                    reason=item.get("reason", ""),
                )
            )
            if not rules[-1].actions or not rules[-1].resources:
                raise ConfigError(f"policy rule {index} has an empty actions/resources list")
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid policy in {path}: {exc}") from exc
    return Policy(rules=tuple(rules), default=default, schema_version=version)


def load_policy_or_default(path: Path) -> tuple[Policy, bool]:
    if path.is_file():
        return load_policy(path), True
    return default_policy(), False


def write_default_policy(path: Path) -> None:
    atomic_write_json(path, default_policy().to_dict())


class PolicyEngine:
    def __init__(self, policy: Policy, *, root: Path | None = None):
        self.policy = policy
        self.root = root

    def matching_rules(self, action: str, resource: str) -> list[PolicyRule]:
        return [rule for rule in self.policy.rules if rule.matches(action, resource)]

    def decide(self, action: str, resource: str) -> Decision:
        normalized = resource.replace("\\", "/")
        matched = self.matching_rules(action, normalized)
        hard = [rule for rule in matched if rule.effect == "hard_deny"]
        if hard:
            rule = hard[-1]
            return Decision(
                action=action,
                resource=normalized,
                effect="hard_deny",
                risk="critical",
                rule_id=rule.id,
                reason=rule.reason,
                matched_rules=tuple(item.id for item in matched),
            )
        if matched:
            rule = matched[-1]
            return Decision(
                action=action,
                resource=normalized,
                effect=rule.effect,
                risk=rule.risk,
                rule_id=rule.id,
                reason=rule.reason,
                matched_rules=tuple(item.id for item in matched),
            )
        return Decision(
            action=action,
            resource=normalized,
            effect=self.policy.default,  # type: ignore[arg-type]
            risk="medium",
            rule_id=None,
            reason="no rule matched; policy default applies",
            matched_rules=(),
        )

    def authorize(
        self,
        action: str,
        resource: str,
        *,
        approved: bool = False,
        interactive: bool = False,
    ) -> Decision:
        decision = self.decide(action, resource)
        if decision.effect == "allow":
            return decision
        if decision.effect in {"deny", "hard_deny"}:
            raise PolicyDenied(
                f"{action} denied for {decision.resource}",
                hint=decision.reason or "policy rule denied this action",
                details=decision.to_dict(),
            )
        # ask
        if approved:
            return Decision(
                action=decision.action,
                resource=decision.resource,
                effect="allow",
                risk=decision.risk,
                rule_id=decision.rule_id,
                reason=f"{decision.reason} (approved once)".strip(),
                matched_rules=decision.matched_rules,
            )
        if interactive:
            return decision
        raise PolicyDenied(
            f"approval required for {action} on {decision.resource}",
            hint="non-interactive execution treats ask as deny; rerun with approval",
            details=decision.to_dict(),
        )
