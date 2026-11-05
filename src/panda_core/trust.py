"""User-scoped trust in a workspace policy file (R2-113).

`.panda/policy.json` travels with the corpus, so its `allow` rules are corpus data
configuring the control that should contain the corpus. Unless the user trusts the
file, a workspace policy can only tighten the built-in policy. Trust is stored
outside every workspace and is bound to the file's SHA-256: editing the file
withdraws it.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from panda_core.atomic import atomic_write_json
from panda_core.safe_read import read_workspace_file

TRUST_ENV = "PANDA_TRUST_FILE"
_MAX_POLICY_BYTES = 1_000_000


def trust_path() -> Path:
    override = os.environ.get(TRUST_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from panda_core.workspace import home_workspace_path

    return home_workspace_path().parent / "trust.json"


def policy_sha256(root: Path, policy_path: Path) -> str | None:
    """SHA-256 of the policy file as Panda reads it, or None if it cannot be read."""

    try:
        text = read_workspace_file(root, policy_path, max_bytes=_MAX_POLICY_BYTES)
    except Exception:  # noqa: BLE001 - an unreadable file is never trusted
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _key(root: Path) -> str:
    return os.path.realpath(root)


def _load() -> dict[str, Any]:
    try:
        data = json.loads(trust_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_trusted(root: Path, policy_path: Path) -> bool:
    entry = _load().get(_key(root))
    digest = policy_sha256(root, policy_path)
    return bool(isinstance(entry, dict) and digest and entry.get("policy_sha256") == digest)


def grant(root: Path, policy_path: Path) -> str:
    digest = policy_sha256(root, policy_path)
    if digest is None:
        raise ValueError(f"cannot read {policy_path}")
    data = _load()
    data[_key(root)] = {
        "policy_sha256": digest,
        "granted_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path = trust_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)
    return digest


def revoke(root: Path) -> bool:
    data = _load()
    if data.pop(_key(root), None) is None:
        return False
    atomic_write_json(trust_path(), data)
    return True


# Allow rules that earlier built-in policies wrote into `.panda/policy.json`
# (`panda init` up to 0.2.0a5). They loosen nothing a user decided, so they never
# ask for trust; under the stricter-of rule they are simply no-ops.
_LEGACY_ALLOW = frozenset(
    {
        ("shell-readonly", ("shell",), (
            "git status*", "git diff*", "git log*", "git show*", "git branch*",
            "panda validate*",
        )),
        # DEAD-001: families removed from the built-in policy in 0.2.0a6.
        ("git-readonly", ("git",), ("status*", "diff*", "log*", "show*", "branch*")),
        ("release-build", ("release",), ("build*",)),
    }
)


def loosening_rules(policy: Any) -> list[Any]:
    """`allow` rules of a workspace policy that the built-in policy does not have.

    Only these change anything when the file is trusted, so they are what the
    user is asked to read before trusting it.
    """

    from panda_core.policy import default_policy

    built_in = set(default_policy().rules)
    found = []
    for rule in policy.rules:
        if rule.effect != "allow" or rule in built_in:
            continue
        if (rule.id, tuple(rule.actions), tuple(rule.resources)) in _LEGACY_ALLOW:
            continue
        found.append(rule)
    return found


def is_wildcard(rule: Any) -> bool:
    """A rule that allows a whole action (`shell *`, `write *`)."""

    return any(resource.strip() in {"*", "**", "*/*"} for resource in rule.resources) or any(
        action.strip() == "*" for action in rule.actions
    )
