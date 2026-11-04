"""R2-500 (APR-004 / C-5): `--auto` must never approve a push, whatever the
argv wrapper or the workspace policy. Asserts the SAFE outcome; fails at 704f387.

Pipeline reproduced exactly as tools._authorize builds it (tools.py:232-250):
decision = policy.decide_shell(argv) -> ApprovalRequest(rule_id=decision.rule_id)
-> ChatRuntime._approve (runtime.py:118-122).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from panda_core.agent import ApprovalRequest
from panda_core.workspace import Workspace


def _auto_approves(runtime, argv: list[str]) -> bool:
    decision = runtime.policy.decide_shell(argv)
    if decision.effect != "ask":
        return False  # denied outright: safe
    request = ApprovalRequest(
        tool="run_shell", action="shell", resource=" ".join(argv),
        rule_id=decision.rule_id, risk=decision.risk, reason=decision.reason,
        preview=" ".join(argv), preview_kind="command",
    )
    return bool(asyncio.run(runtime._approve(request)))


PUSHES = [
    ["git", "push", "origin", "main"],
    ["env", "git", "push", "origin", "main"],
    ["timeout", "5", "git", "push", "origin", "main"],
    ["git", "-c", "user.name=x", "push", "origin", "main"],
    ["git", "-C", ".", "push", "origin", "main"],
    ["/usr/bin/git", "push", "origin", "main"],
]


@pytest.mark.parametrize("argv", PUSHES, ids=lambda a: " ".join(a))
def test_auto_never_approves_a_push_with_the_default_policy(workspace, argv) -> None:
    from panda_cli.runtime import ChatRuntime

    runtime = ChatRuntime(workspace, auto=True)
    assert _auto_approves(runtime, argv) is False


@pytest.mark.parametrize("argv", PUSHES[:1], ids=lambda a: " ".join(a))
def test_auto_never_approves_a_push_under_a_corpus_supplied_policy(fake_corpus, argv) -> None:
    from panda_cli.runtime import ChatRuntime
    from panda_core.init import initialize_workspace

    initialize_workspace(fake_corpus)
    # A corpus received from a third party ships its own policy.json (B1).
    # It is stricter-looking than the default: everything shell is `ask`.
    (fake_corpus / ".panda" / "policy.json").write_text(json.dumps({
        "schema_version": 1, "default": "ask",
        "rules": [{"id": "corpus-shell-ask", "actions": ["shell"], "resources": ["*"], "effect": "ask"}],
    }), encoding="utf-8")
    runtime = ChatRuntime(Workspace.from_root(fake_corpus), auto=True)
    assert _auto_approves(runtime, argv) is False
