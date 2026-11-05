"""Fail-closed policy engine.

Rules are evaluated in order and the last match wins. `hard_deny` rules are
non-overridable: they win even if a later rule would allow the action. This is
what makes secret access and destructive git operations impossible to approve
away. An `ask` decision outside an interactive prompt is a denial.
"""

from __future__ import annotations

import fnmatch
import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from panda_core.atomic import atomic_write_json
from panda_core.errors import ConfigError, PolicyDenied
from panda_core.safe_read import read_workspace_file
from panda_core.shell_policy import SEVERITY, ShellVerdict, classify_shell

Effect = Literal["allow", "deny", "ask", "hard_deny"]
Risk = Literal["low", "medium", "high", "critical"]

_SCHEMA_VERSION = 1
RISKS: tuple[str, ...] = ("low", "medium", "high", "critical")
_CATCH_ALL = frozenset({"shell-default-ask", None})


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

    def matches_argv(self, action: str, argv: Sequence[str]) -> bool:
        """Token-prefix match: `git status*` matches argv `git status --short`.

        The program token must be a plain name, so a directory called `git status`
        cannot make `git status/../../usr/bin/touch` look like `git status`.
        """

        if not any(fnmatch.fnmatchcase(action.lower(), a.lower()) for a in self.actions):
            return False
        if not argv or not _plain_program(argv[0]):
            return False
        for pattern in self.resources:
            tokens = _split(pattern)
            if not tokens:
                continue
            open_end = tokens[-1].endswith("*")
            if tokens[-1] == "*":
                tokens = tokens[:-1]
            if len(argv) < len(tokens) or (not open_end and len(argv) != len(tokens)):
                continue
            if all(fnmatch.fnmatchcase(arg, tok) for arg, tok in zip(argv, tokens, strict=False)):
                return True
        return False

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
    # For shell, the classifier's effect class; for other actions, the action.
    effect_class: str = ""

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
            "effect_class": self.effect_class or self.action,
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
            ["*id_rsa*", "*id_ed25519*", "*id_ecdsa*", "*id_dsa*", "*/.ssh/*",
             "*/.aws/credentials"],
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
                # R2-108: common credential files the list missed.
                ".envrc", "*/.envrc", ".kube/config", "*/.kube/config",
                "application_default_credentials.json", "*/application_default_credentials.json",
                ".vault-token", "*/.vault-token", "*.tfstate", "*.tfstate.*", ".s3cfg",
                "*/.s3cfg", ".boto", "*/.boto", ".htpasswd", "*/.htpasswd",
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
        # Every shell command asks. Only the argv classifier grants `allow`, for the
        # closed set of read-only git forms it understands (R2-100, R2-104): a text
        # glob such as `git status*` also matched `git status/../../usr/bin/touch`.
        _rule("shell-default-ask", ["shell"], ["*"], "ask", "medium"),
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
    [
        ".panda/*", "corpus/validation/*",
        # A nested workspace is loaded by `--root` (R2-104).
        "*/.panda/*", "*/corpus/validation/*",
        # Node resolves the validator's imports from these (R2-210).
        "node_modules/*", "corpus/node_modules/*", "package.json", "corpus/package.json",
        ".npmrc", "corpus/.npmrc",
    ],
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
        rules = [_parse_rule(index, item) for index, item in enumerate(raw.get("rules", []))]
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid policy in {path}: {exc}") from exc
    return Policy(rules=tuple(rules), default=default, schema_version=version)


_RULE_KEYS = frozenset({"id", "actions", "resources", "effect", "risk", "reason"})


def _string_list(index: int, item: dict, key: str) -> tuple[str, ...]:
    value = item.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        # A bare string used to become a tuple of letters (R2-116).
        raise ConfigError(f"policy rule {index}: {key!r} must be a non-empty list of strings")
    return tuple(value)


def _parse_rule(index: int, item: Any) -> PolicyRule:
    if not isinstance(item, dict):
        raise ConfigError(f"policy rule {index} must be an object")
    unknown = set(item) - _RULE_KEYS
    if unknown:
        raise ConfigError(f"policy rule {index} has unknown fields: {', '.join(sorted(unknown))}")
    if item.get("effect") not in _EFFECTS:
        raise ConfigError(
            f"policy rule {index} has an invalid effect: {item.get('effect')!r}",
            hint=f"use one of {', '.join(sorted(_EFFECTS))}",
        )
    risk = item.get("risk", "low")
    if risk not in RISKS:
        raise ConfigError(
            f"policy rule {index} has an invalid risk: {risk!r}",
            hint=f"use one of {', '.join(RISKS)}",
        )
    rule_id, reason = item.get("id"), item.get("reason", "")
    if not isinstance(rule_id, str) or not rule_id or not isinstance(reason, str):
        raise ConfigError(f"policy rule {index}: 'id' and 'reason' must be strings")
    return PolicyRule(
        id=rule_id,
        actions=_string_list(index, item, "actions"),
        resources=_string_list(index, item, "resources"),
        effect=item["effect"],
        risk=risk,
        reason=reason,
    )


def load_policy_or_default(path: Path) -> tuple[Policy, bool]:
    if path.is_file():
        return load_policy(path), True
    return default_policy(), False


def write_default_policy(path: Path) -> None:
    atomic_write_json(path, default_policy().to_dict())


class PolicyEngine:
    """Decides actions from rules, the built-in floor and the shell classifier.

    `policy` is the authority. With `baseline`, every decision is the stricter of
    `policy` and `baseline`: that is how a workspace file that is not trusted can
    only tighten the built-in policy (R2-113). `protected` adds write hard denies
    for paths that belong to this workspace's control plane (its validator).
    """

    def __init__(
        self,
        policy: Policy,
        *,
        root: Path | None = None,
        baseline: Policy | None = None,
        protected: Sequence[str] = (),
    ):
        self.policy = policy
        self.root = root
        self.baseline = baseline
        floor = list(policy_floor())
        if protected:
            floor.append(
                _rule(
                    "floor-validator",
                    ["write", "edit"],
                    list(protected),
                    "hard_deny",
                    "critical",
                    "the configured validator and its directory are not agent-writable",
                )
            )
        self._floor = tuple(floor)

    def matching_rules(
        self,
        action: str,
        resource: str,
        *,
        argv: Sequence[str] | None = None,
        rules: Sequence[PolicyRule] | None = None,
    ) -> list[PolicyRule]:
        matched: list[PolicyRule] = []
        source = self.policy.rules if rules is None else rules
        for rule in (*_BASELINE_RULES, *source, *self._floor):
            # Identical rules (the built-in policy already contains the floor)
            # are listed once; a weaker rule with the same id is kept and loses.
            if rule in matched:
                continue
            if action.lower() == "shell" and rule.effect == "allow":
                # A shell allow never matches joined text, only argv tokens (R2-100).
                tokens = argv if argv is not None else _split(resource)
                if rule.matches_argv(action, tokens):
                    matched.append(rule)
            elif rule.matches(action, resource):
                matched.append(rule)
        return matched

    def decide(self, action: str, resource: str) -> Decision:
        if action == "shell":
            # One tokenizer on every platform: `posix=False` kept the quotes in
            # `git "push" --force` and the classifier missed it (R2-203, R2-503).
            return self.decide_shell(_split(resource), text=resource)
        return self._decide_rules(action, resource)

    def decide_shell(self, argv: Sequence[str], *, text: str | None = None) -> Decision:
        """Decide a command from its argv: rules first, then the token classifier.

        Rules may only ask or deny a shell command, except a trusted workspace allow,
        which matches tokens. The classifier grants `allow` for the read-only forms
        it understands when only the catch-all asked, and otherwise only escalates.
        The result keeps the stricter effect and the higher risk and, on a tie, the
        classifier's rule and reason, which describe what the command does (R2-500:
        `env git push` used to report `shell-default-ask`).
        """

        base = self._decide_rules(
            "shell", text if text is not None else " ".join(argv), argv=argv
        )
        verdict = classify_shell(argv)
        if verdict is None:
            return replace(base, effect_class="exec_unknown")
        worst = verdict
        for path in verdict.read_paths:
            read = self._decide_rules("read", path)
            if read.effect != "allow":
                worst = _stricter_verdict(
                    worst,
                    ShellVerdict(
                        read.effect,
                        read.risk,
                        f"reads {path}: {read.reason}",
                        read.rule_id or "shell-git-read",
                    ),
                )
        grantable = bool(argv) and argv[0] in {"git", "git.exe"}
        if (
            grantable
            and worst.effect == "allow"
            and base.effect == "ask"
            and base.rule_id in _CATCH_ALL
        ):
            # Only the catch-all asked and the program is a plain `git`, which the
            # tool runs isolated (gitsafe): the classifier knows it is read-only.
            # A wrapper (`env git status`) or a path (`/usr/bin/git`) still asks.
            return Decision(
                action="shell",
                resource=base.resource,
                effect="allow",
                risk=worst.risk,  # type: ignore[arg-type]
                rule_id=worst.rule_id,
                reason=worst.reason,
                matched_rules=base.matched_rules,
                effect_class=verdict.effect_class,
            )
        if SEVERITY[worst.effect] < SEVERITY[base.effect]:
            return replace(base, effect_class=verdict.effect_class)
        return Decision(
            action="shell",
            resource=base.resource,
            effect=worst.effect,  # type: ignore[arg-type]
            risk=max(base.risk, worst.risk, key=RISKS.index),  # type: ignore[arg-type]
            rule_id=worst.rule_id,
            reason=worst.reason,
            matched_rules=base.matched_rules,
            effect_class=_class_of(verdict, worst),
        )

    def rule_source(self, rule_id: str | None) -> str:
        """Where a rule comes from, for the consent card: built-in, workspace or classifier.

        A workspace rule that is not identical to the built-in rule of the same id is
        labelled `workspace`, so its reason is read as corpus text (R2-207).
        """

        if not rule_id:
            return "default"
        built_in = {rule.id: rule for rule in (*default_policy().rules, *self._floor)}
        for rule in self.policy.rules:
            if rule.id == rule_id and built_in.get(rule_id) != rule:
                return "workspace"
        if rule_id in built_in or rule_id in _BUILT_IN_IDS:
            return "built-in"
        return "classifier"

    def denied_read_patterns(self) -> tuple[str, ...]:
        """Globs of every read rule that does not allow: excluded from git output."""

        patterns: list[str] = []
        for rule in (*_BASELINE_RULES, *self.policy.rules, *self._floor):
            if rule.effect != "allow" and any(a in {"read", "*"} for a in rule.actions):
                patterns.extend(rule.resources)
        return tuple(dict.fromkeys(patterns))

    def _decide_rules(
        self, action: str, resource: str, *, argv: Sequence[str] | None = None
    ) -> Decision:
        decision = self._evaluate(self.policy, action, resource, argv)
        if self.baseline is not None:
            decision = _stricter(decision, self._evaluate(self.baseline, action, resource, argv))
        return decision

    def _evaluate(
        self, policy: Policy, action: str, resource: str, argv: Sequence[str] | None
    ) -> Decision:
        normalized = resource.replace("\\", "/")
        matched = self.matching_rules(action, normalized, argv=argv, rules=policy.rules)
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
            effect=policy.default,  # type: ignore[arg-type]
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


def _split(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def _stricter(first: Decision, second: Decision) -> Decision:
    """The more restrictive decision; on the same effect, the higher risk."""

    key_first = (SEVERITY[first.effect], RISKS.index(first.risk))
    key_second = (SEVERITY[second.effect], RISKS.index(second.risk))
    return second if key_second > key_first else first


def _stricter_verdict(first: ShellVerdict, second: ShellVerdict) -> ShellVerdict:
    return second if SEVERITY[second.effect] > SEVERITY[first.effect] else first


def _plain_program(token: str) -> bool:
    return bool(token) and not any(mark in token for mark in ("/", "\\", " ", "\t")) and (
        token not in {".", ".."}
    )


def engine_for(workspace: Any, policy: Policy, *, from_file: bool) -> PolicyEngine:
    """The engine every surface uses for a workspace (one construction point).

    A policy file in the workspace is corpus data: unless the user trusts it, it
    can only tighten the built-in policy. The configured validator is protected.
    """

    from panda_core.trust import is_trusted  # local: trust reads user state

    baseline = None
    if from_file and not is_trusted(workspace.root, workspace.policy_path):
        baseline = default_policy()
    protected: list[str] = []
    try:
        validator = Path(workspace.config.validator)
        rel = validator.as_posix().lstrip("./")
        if rel and not validator.is_absolute() and ".." not in validator.parts:
            protected.append(rel)
            if validator.parent.as_posix() not in {"", "."}:
                protected.append(f"{validator.parent.as_posix()}/*")
    except (AttributeError, TypeError, ValueError):
        pass
    return PolicyEngine(policy, root=workspace.root, baseline=baseline, protected=protected)


def _class_of(verdict: ShellVerdict, worst: ShellVerdict) -> str:
    """A command that reads a denied path is still the command it is, unless denied."""

    return worst.effect_class if SEVERITY[worst.effect] > SEVERITY[verdict.effect] else (
        verdict.effect_class
    )


_BUILT_IN_IDS = frozenset(rule.id for rule in (*default_policy().rules, *_BASELINE_RULES))

