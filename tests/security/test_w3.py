"""Wave 3: the validator contract (VAL-001) and its environment (VAL-002)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panda_core.errors import ProtocolError
from panda_core.validator import CHECK_NAMES, UNCONDITIONAL_CHECKS, Validator, parse_report


def report(checks: list[dict], *, ok: bool | None = None) -> str:
    computed = all(c["ok"] or c.get("skipped") for c in checks)
    return json.dumps({"ok": computed if ok is None else ok, "checks": checks})


def check(name: str, *, ok: bool = True, skipped: bool = False) -> dict:
    return {"name": name, "ok": ok, "skipped": skipped, "reason": "", "errors": []}


def all_checks(**overrides: dict) -> list[dict]:
    return [overrides.get(name, check(name)) for name in CHECK_NAMES]


# --------------------------------------------------------------------------- VAL-001
def test_an_empty_report_is_a_contract_failure() -> None:
    with pytest.raises(ProtocolError):
        parse_report('{"ok":true,"checks":[]}')


def test_a_report_where_every_check_is_skipped_is_a_contract_failure() -> None:
    skipped = [check(name, skipped=True) for name in CHECK_NAMES]
    with pytest.raises(ProtocolError):
        parse_report(report(skipped))


@pytest.mark.parametrize("name", UNCONDITIONAL_CHECKS)
def test_each_unconditional_check_must_be_present(name: str) -> None:
    checks = [c for c in all_checks() if c["name"] != name]
    with pytest.raises(ProtocolError) as info:
        parse_report(report(checks))
    assert name in info.value.message


@pytest.mark.parametrize("name", UNCONDITIONAL_CHECKS)
def test_each_unconditional_check_must_run(name: str) -> None:
    checks = all_checks(**{name: check(name, skipped=True)})
    with pytest.raises(ProtocolError) as info:
        parse_report(report(checks))
    assert name in info.value.message


def test_the_conditional_check_may_be_skipped_or_missing() -> None:
    face = check("public-face", skipped=True)
    skipped = parse_report(report(all_checks(**{"public-face": face})))
    assert skipped.ok
    missing = parse_report(report([c for c in all_checks() if c["name"] != "public-face"]))
    assert missing.ok and any("public-face" in w for w in missing.warnings)


def test_neg_a_complete_report_and_a_real_failure_still_parse() -> None:
    assert parse_report(report(all_checks())).ok is True
    failed = parse_report(report(all_checks(evidence=check("evidence", ok=False))))
    assert failed.ok is False


def test_neg_an_inconsistent_report_is_still_refused() -> None:
    with pytest.raises(ProtocolError):
        parse_report(report(all_checks(evidence=check("evidence", ok=False)), ok=True))


def test_a_validator_that_prints_an_empty_report_fails_end_to_end(workspace) -> None:
    workspace.validator_path.write_text(
        'console.log(JSON.stringify({ok:true,checks:[]}));\n', encoding="utf-8"
    )
    with pytest.raises(ProtocolError):
        Validator(workspace.root, script=workspace.validator_path).run()


# --------------------------------------------------------------------------- VAL-002
def test_the_validator_does_not_inherit_credentials(
    workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = tmp_path / "env-seen.json"
    names = [name for name in CHECK_NAMES]
    checks = json.dumps([{"name": n, "ok": True, "skipped": False, "errors": []} for n in names])
    workspace.validator_path.write_text(
        'import { writeFileSync } from "node:fs";\n'
        f"writeFileSync({json.dumps(str(seen))}, JSON.stringify(process.env));\n"
        f"console.log(JSON.stringify({{ok:true,checks:{checks}}}));\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_API_KEY", "SYNTH_SECRET_PROVIDER")
    monkeypatch.setenv("GITHUB_TOKEN", "SYNTH_SECRET_GH")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SYNTH_SECRET_AWS")
    monkeypatch.setenv("DB_PASSWORD", "SYNTH_SECRET_DB")
    monkeypatch.setenv("PANDA_API_KEY", "SYNTH_SECRET_PANDA")
    monkeypatch.setenv("PANDA_FAKE_SETTING", "kept")
    Validator(workspace.root, script=workspace.validator_path).run()
    env = json.loads(seen.read_text(encoding="utf-8"))
    for name in ("OPENCODE_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD",
                 "PANDA_API_KEY"):
        assert name not in env, name
    assert "SYNTH_SECRET" not in json.dumps(env)
    assert env.get("PANDA_FAKE_SETTING") == "kept"
    assert "PATH" in env
