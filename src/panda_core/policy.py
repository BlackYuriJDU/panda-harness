"""Fail-closed policy engine.

Rules are evaluated in order and the last match wins. `hard_deny` rules are
non-overridable: they win even if a later rule would allow the action. This is
what makes secret access and destructive git operations impossible to approve
away. An `ask` decision outside an interactive prompt is a denial.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from panda_core.atomic import atomic_write_json
from panda_core.errors import ConfigError, PolicyDenied
from panda_core.safe_read import read_workspace_file
from panda_core.shell_policy import SEVERITY, ShellVerdict, classify_shell

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


# Applied beneath every workspace policy, so a policy written before the `validate`
# action existed keeps working; a workspace rule for the action overrides it.
_BASELINE_RULES = (
    _rule(
        "validate-default",
        ["validate"],
        ["*"],
        "allow",
        "low",
        "runs the workspace validator, which the agent cannot write",
    ),
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
            "read-credential-files",
            ["read"],
            [
                ".git/config", "*/.git/config", ".netrc", "*/.netrc", "_netrc", "*/_netrc",
                ".npmrc", "*/.npmrc", ".pypirc", "*/.pypirc", ".pgpass", "*/.pgpass",
                ".git-credentials", "*/.git-credentials", "credentials.json",
                "*/credentials.json", ".aws/credentials", ".ssh/*", ".docker/config.json",
                "*/.docker/config.json", "*.p12", "*.pfx", "*.kdbx", "*.keystore", "*.jks",
                "secrets.*", "*/secrets.*",
            ],
            "hard_deny",
            "critical",
            "credential file",
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
        # MCP tool calls. `validate` and `study_show` also pass through the registry
        # policy (validate / read); the other exposed tools only describe state.
        # FastMCP registers the plain names, so both spellings are listed.
        _rule("tool-default-ask", ["tool"], ["*"], "ask", "medium"),
        _rule(
            "tool-panda-readonly",
            ["tool"],
            [
                "workspace_info",
                "validate",
                "policy_check",
                "study_list",
                "study_show",
                "session_note",
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
        _BASELINE_RULES[0],
        # Anything not named above is an explicit decision.
        _rule("external-default-ask", ["external_read", "external_write"], ["*"], "ask", "high"),
    )
    return Policy(rules=rules, default="ask")


# Actions a tool or the agent really consults, and actions the default policy declares but
# nothing consults yet. A rule for a planned action protects nothing today (DEAD-001): a
# `release-publish` hard deny, for instance, guards a tool that does not exist.
ENFORCED_ACTIONS = frozenset({"read", "write", "edit", "shell", "validate"})
PLANNED_ACTIONS = frozenset(
    {
        "delete",
        "git",
        "network",
        "webfetch",
        "websearch",
        "release",
        "spend",
        "tool",
        "question",
        "external_read",
        "external_write",
    }
)

_EFFECTS = frozenset({"allow", "ask", "deny", "hard_deny"})

_CONTROL_PLANE_RULE = _rule(
    "floor-control-plane",
    ["write", "edit"],
    [".panda/*", "corpus/validation/*"],
    "hard_deny",
    "critical",
    "control plane: policy, config, ledger and validator are not agent-writable",
)


_GIT_DIR_RULE = _rule(
    "floor-git-dir",
    ["write", "edit"],
    [".git", ".git/*", ".git[. :]*", "*/.git", "*/.git/*", "*/.git[. :]*", "git~*", "*/git~*"],
    "hard_deny",
    "critical",
    "a .git directory names programs (hooks, filters, fsmonitor) that git runs",
)


def policy_floor() -> tuple[PolicyRule, ...]:
    """Hard denies that no workspace policy can remove (TRUST-003).

    `.panda/policy.json` travels with the corpus, so a hostile one could allow
    everything. The floor is every `hard_deny` of the built-in policy plus the
    control plane, and is evaluated beside the workspace rules: `hard_deny`
    always wins, so the workspace can only tighten.
    """

    built_in = tuple(rule for rule in default_policy().rules if rule.effect == "hard_deny")
    return (*built_in, _CONTROL_PLANE_RULE, _GIT_DIR_RULE)


def load_policy(path: Path) -> Policy:
    if not path.is_file():
        raise ConfigError(
            f"policy not found: {path}",
            hint="run `panda init` to generate the default policy",
        )
    try:
        raw = json.loads(read_workspace_file(path.parent.parent, path))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid policy JSON in {path}: {exc.msg}") from exc
    try:
        version = int(raw.get("schema_version", 0))
        if version != _SCHEMA_VERSION:
            raise ConfigError(f"unsupported policy schema_version: {version}")
        default = raw.get("default", "ask")
        if default not in {"allow", "deny", "ask"}:
            raise ConfigError(f"invalid policy default: {default}")
        if default == "allow":
            raise ConfigError(
                f"policy default 'allow' is not accepted from a workspace file: {path}",
                hint="use 'ask' or 'deny'; a workspace may only tighten the policy",
            )
        rules: list[PolicyRule] = []
        for index, item in enumerate(raw.get("rules", [])):
            if item.get("effect") not in _EFFECTS:
                raise ConfigError(
                    f"policy rule {index} has an invalid effect: {item.get('effect')!r}",
                    hint=f"use one of {', '.join(sorted(_EFFECTS))}",
                )
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
        self._floor = policy_floor()

    def matching_rules(self, action: str, resource: str) -> list[PolicyRule]:
        matched: list[PolicyRule] = []
        for rule in (*_BASELINE_RULES, *self.policy.rules, *self._floor):
            # Identical rules (the built-in policy already contains the floor)
            # are listed once; a weaker rule with the same id is kept and loses.
            if rule.matches(action, resource) and rule not in matched:
                matched.append(rule)
        return matched

    def decide(self, action: str, resource: str) -> Decision:
        if action == "shell":
            try:
                argv = shlex.split(resource, posix=os.name != "nt")
            except ValueError:
                argv = resource.split()
            return self.decide_shell(argv, text=resource)
        return self._decide_rules(action, resource)

    def decide_shell(self, argv: Sequence[str], *, text: str | None = None) -> Decision:
        """Decide a command from its argv: rules first, then the token classifier.

        The classifier can only escalate (SEC-001, SEC-007), so a workspace policy
        can tighten the result but never loosen it.
        """

        base = self._decide_rules("shell", text if text is not None else " ".join(argv))
        verdict = classify_shell(argv)
        if verdict is None:
            return base
        worst = verdict
        for path in verdict.read_paths:
            read = self._decide_rules("read", path)
            if read.effect != "allow":
                worst = max(
                    (worst, ShellVerdict(read.effect, read.risk, f"reads {path}: {read.reason}",
                                         read.rule_id or "shell-git-read")),
                    key=lambda item: SEVERITY[item.effect],
                )
        if SEVERITY[worst.effect] <= SEVERITY[base.effect]:
            return base
        return Decision(
            action="shell",
            resource=base.resource,
            effect=worst.effect,  # type: ignore[arg-type]
            risk=worst.risk,  # type: ignore[arg-type]
            rule_id=worst.rule_id,
            reason=worst.reason,
            matched_rules=(*base.matched_rules, worst.rule_id),
        )

    def denied_read_patterns(self) -> tuple[str, ...]:
        """Globs of every read rule that does not allow: excluded from git output."""

        patterns: list[str] = []
        for rule in (*_BASELINE_RULES, *self.policy.rules, *self._floor):
            if rule.effect != "allow" and any(a in {"read", "*"} for a in rule.actions):
                patterns.extend(rule.resources)
        return tuple(dict.fromkeys(patterns))

    def _decide_rules(self, action: str, resource: str) -> Decision:
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
