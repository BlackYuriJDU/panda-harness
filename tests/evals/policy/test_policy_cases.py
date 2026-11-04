"""Adversarial policy cases, run through the real route (ChatRuntime -> ToolRegistry).

Every bypass found by an audit becomes a data case in `cases/*.yaml`
(schema `panda.eval.policy.v1`). A case states the most permissive effect that is
acceptable, whether the command may run, and side effects that must not happen.
A case that documents a hole not fixed yet carries `open_until: <wave>`; it runs as
a strict xfail, so fixing the hole without removing the marker fails the suite.

Synthetic data only; everything lives in tmp_path; no provider is called.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from panda_core import tools as tools_module
from panda_core.policy import SEVERITY

CASES_DIR = Path(__file__).parent / "cases"
SCHEMA = "panda.eval.policy.v1"
APPROVERS = {"none", "deny", "approve", "auto"}
ROUTES = {"run_shell", "decide", "decide_shell"}


def _load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(CASES_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert document.get("schema") == SCHEMA, f"{path.name}: schema must be {SCHEMA}"
        for case in document["cases"]:
            assert case["id"] not in seen, f"duplicate case id {case['id']}"
            seen.add(case["id"])
            assert case["route"] in ROUTES, case["id"]
            assert case.get("approver", "none") in APPROVERS, case["id"]
            case["_file"] = path.name
            cases.append(case)
    return cases


CASES = _load_cases()


def _params() -> list[Any]:
    params = []
    for case in CASES:
        marks = []
        if case.get("open_until"):
            marks.append(
                pytest.mark.xfail(strict=True, reason=f"open until {case['open_until']}")
            )
        if case.get("posix_only") and sys.platform == "win32":
            marks.append(pytest.mark.skip(reason="POSIX-only case"))
        params.append(pytest.param(case, id=case["id"], marks=marks))
    return params


# --------------------------------------------------------------------------- setup steps
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=x@example.invalid", "-c", "user.name=x", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _payload(tmp: Path, marker: Path) -> Path:
    script = tmp / "payload.sh"
    script.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{marker}'\ncat\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _hostile_git(root: Path, tmp: Path, marker: Path) -> None:
    """Repository config that names programs: fsmonitor, external diff, textconv, filter."""

    script = _payload(tmp, marker)
    _git(root, "init", "-q", "-b", "main")
    (root / "notes.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "notes.txt")
    _git(root, "commit", "-q", "-m", "seed")
    (root / ".gitattributes").write_text("*.txt diff=evil filter=evil\n", encoding="utf-8")
    for key, value in {
        "core.fsmonitor": script,
        "diff.external": script,
        "diff.evil.textconv": script,
        "filter.evil.clean": script,
    }.items():
        _git(root, "config", key, str(value))
    (root / "notes.txt").write_text("two\n", encoding="utf-8")


def _nested_hostile_git(root: Path, tmp: Path, marker: Path) -> None:
    """What an agent can create with write_file alone: corpus/sub/.git/config."""

    script = _payload(tmp, marker)
    sub = root / "corpus" / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    _git(sub, "init", "-q", "-b", "main")
    _git(sub, "config", "core.fsmonitor", str(script))


def _committed_env(root: Path, tmp: Path, marker: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    (root / ".env").write_text("TOKEN=SYNTH_SECRET_GITDIFF_4d2a\n", encoding="utf-8")
    (root / "notes.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-f", ".env", "notes.txt")
    _git(root, "commit", "-q", "-m", "seed")
    (root / ".env").write_text("TOKEN=SYNTH_SECRET_GITDIFF_4d2a_changed\n", encoding="utf-8")


def _info_attributes_filter(root: Path, tmp: Path, marker: Path) -> None:
    """The attribute source that --attr-source does not replace, with a macro."""

    script = _payload(tmp, marker)
    _git(root, "init", "-q", "-b", "main")
    (root / "notes.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "notes.txt")
    _git(root, "commit", "-q", "-m", "seed")
    info = root / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text(
        "[attr]innocent filter=evil.x\n*.txt innocent\n", encoding="utf-8"
    )
    _git(root, "config", "filter.evil.x.clean", str(script))
    (root / "notes.txt").write_text("two\n", encoding="utf-8")
    os.utime(root / "notes.txt", (1, 1))


def _parent_repo(root: Path, tmp: Path, marker: Path) -> None:
    """The workspace has no .git, but a hostile repository encloses it."""

    script = _payload(tmp, marker)
    _git(tmp, "init", "-q", "-b", "main")
    _git(tmp, "config", "core.fsmonitor", str(script))


def _git_symlink(root: Path, tmp: Path, marker: Path) -> None:
    elsewhere = tmp / "elsewhere"
    _git(tmp, "init", "-q", "-b", "main", str(elsewhere))
    _git(elsewhere, "config", "core.fsmonitor", str(_payload(tmp, marker)))
    (root / ".git").symlink_to(elsewhere / ".git", target_is_directory=True)


SETUPS = {
    "info_attributes_filter": _info_attributes_filter,
    "parent_repo": _parent_repo,
    "git_symlink": _git_symlink,
    "hostile_git": _hostile_git,
    "nested_hostile_git": _nested_hostile_git,
    "committed_env": _committed_env,
}


def _expand(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in mapping.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, list):
        return [_expand(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, mapping) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------- runner
def _runtime(workspace, approver: str):
    from panda_cli.runtime import ChatRuntime

    async def yes(request) -> bool:
        return True

    async def no(request) -> bool:
        return False

    if approver == "auto":
        return ChatRuntime(workspace, auto=True, label="eval")
    if approver == "approve":
        return ChatRuntime(workspace, approver=yes, label="eval")
    if approver == "deny":
        return ChatRuntime(workspace, approver=no, label="eval")
    return ChatRuntime(workspace, label="eval")


@pytest.mark.parametrize("case", _params())
def test_policy_case(case: dict[str, Any], workspace, tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "effect-marker.txt"
    mapping = {"marker": str(marker), "tmp": str(tmp_path)}
    case = _expand(case, mapping)
    for step in case.get("setup", []):
        SETUPS[step](workspace.root, tmp_path, marker)
    for relative in case.get("make_dirs", []):
        (workspace.root / relative).mkdir(parents=True, exist_ok=True)
    if case.get("workspace_policy") is not None:
        policy = {"schema_version": 1, "default": "ask", "rules": case["workspace_policy"]}
        workspace.policy_path.write_text(json.dumps(policy), encoding="utf-8")
    if case.get("trusted"):
        from panda_core.trust import grant

        grant(workspace.root, workspace.policy_path)
        if case.get("edit_after_trust"):
            with workspace.policy_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
    if marker.exists():
        marker.unlink()  # setup itself may run git programs; only the case counts

    expected = case["expected"]
    if case["route"] in {"decide", "decide_shell"}:
        _check_decision(case, workspace, expected)
        return

    executed: list[list[str]] = []
    real = tools_module.run_command

    def spy(argv, **kwargs):
        executed.append(list(argv))
        if case.get("execute") == "real":
            return real(argv, **kwargs)
        return real([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(tools_module, "run_command", spy)
    runtime = _runtime(workspace, case.get("approver", "none"))
    try:
        result = asyncio.run(runtime.tools.call("run_shell", case["args"]))
    finally:
        runtime.close()

    if "executed" in expected:
        assert bool(executed) is expected["executed"], (executed, result.content)
    if expected.get("no_marker", True):
        assert not marker.exists(), f"side effect happened: {marker.read_text(encoding='utf-8')}"
    for needle in expected.get("not_in_result", []):
        assert needle not in result.content, f"{needle!r} reached the tool result"
    if "max_effect" in expected:
        decision = runtime.policy.decide_shell(case["args"]["argv"])
        _assert_at_most(decision.effect, expected["max_effect"])


def _check_decision(case: dict[str, Any], workspace, expected: dict[str, Any]) -> None:
    from panda_core.policy import engine_for, load_policy_or_default

    policy, from_file = load_policy_or_default(workspace.policy_path)
    engine = engine_for(workspace, policy, from_file=from_file)
    if case["route"] == "decide_shell":
        decision = engine.decide_shell(case["argv"])
    else:
        decision = engine.decide(case["action"], case["resource"])
    if "max_effect" in expected:
        _assert_at_most(decision.effect, expected["max_effect"])
    if "effect" in expected:
        assert decision.effect == expected["effect"], decision
    if "min_risk" in expected:
        order = ["low", "medium", "high", "critical"]
        assert order.index(decision.risk) >= order.index(expected["min_risk"]), decision


def _assert_at_most(observed: str, most_permissive: str) -> None:
    """`observed` must be as restrictive as `most_permissive`, or more."""

    assert SEVERITY[observed] >= SEVERITY[most_permissive], (
        f"decision {observed!r} is more permissive than {most_permissive!r}"
    )


def test_every_case_names_its_source() -> None:
    for case in CASES:
        assert case.get("refs"), f"{case['id']} has no refs"
        assert case.get("expected"), f"{case['id']} has no expected outcome"


def test_cases_are_loaded() -> None:
    assert len(CASES) >= 30
    assert os.environ.get("PANDA_EVALS_SKIP") is None
