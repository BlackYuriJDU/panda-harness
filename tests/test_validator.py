"""Validator adapter: canonical JSON contract, exit mapping, and timeouts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panda_core.errors import ConfigError, ExecutionTimeout, ProtocolError
from panda_core.validator import CHECK_NAMES, Validator, parse_report


def report_payload(ok: bool = True, fail: str | None = None) -> dict:
    checks = []
    for name in CHECK_NAMES:
        failed = name == fail
        checks.append(
            {
                "name": name,
                "ok": not failed,
                "skipped": False,
                "reason": "",
                "errors": [f"forced failure in {name}"] if failed else [],
                "warnings": [],
            }
        )
    return {
        "ok": ok,
        "generated_at": "2026-10-25T12:00:00.000Z",
        "root": "fixture-root",
        "release_mode": False,
        "checks": checks,
    }


def test_parse_valid_report() -> None:
    report = parse_report(json.dumps(report_payload()))
    assert report.ok
    assert report.passed == 10
    assert report.failed == 0
    assert report.skipped == 0
    assert [check.name for check in report.checks] == list(CHECK_NAMES)
    assert report.warnings == ()


def test_parse_failure_report() -> None:
    report = parse_report(json.dumps(report_payload(ok=False, fail="numbers")))
    assert not report.ok
    assert report.failed == 1
    assert report.check("numbers").errors == ("forced failure in numbers",)


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ProtocolError):
        parse_report("node: something went wrong")


def test_parse_rejects_inconsistent_top_level_ok() -> None:
    payload = report_payload(ok=False, fail="claims")
    payload["ok"] = True
    with pytest.raises(ProtocolError):
        parse_report(json.dumps(payload))


def test_parse_rejects_missing_checks() -> None:
    with pytest.raises(ProtocolError):
        parse_report(json.dumps({"ok": True, "generated_at": "x", "checks": None}))


def test_parse_warns_on_missing_and_extra_checks() -> None:
    payload = report_payload()
    payload["checks"] = [check for check in payload["checks"] if check["name"] != "secrets"]
    payload["checks"].append(
        {"name": "custom", "ok": True, "skipped": False, "reason": "", "errors": []}
    )
    report = parse_report(json.dumps(payload))
    assert any("secrets" in warning for warning in report.warnings)
    assert any("custom" in warning for warning in report.warnings)


def test_parse_tolerates_leading_noise() -> None:
    payload = json.dumps(report_payload())
    report = parse_report("warning: deprecated API\n" + payload)
    assert report.ok


def test_validator_runs_fake_corpus(fake_corpus: Path) -> None:
    validator = Validator(fake_corpus, timeout=30)
    report = validator.run()
    assert report.ok
    assert report.passed == 10
    assert report.exit_code == 0
    assert report.duration_ms >= 0
    assert report.node_version and report.node_version.startswith("v")


def test_validator_maps_failure_exit_code(
    fake_corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PANDA_FAKE_FAIL", "evidence")
    report = Validator(fake_corpus, timeout=30).run()
    assert not report.ok
    assert report.exit_code == 1
    assert report.failed == 1
    assert report.check("evidence").errors


def test_validator_release_flag(fake_corpus: Path) -> None:
    report = Validator(fake_corpus, timeout=30).run(release=True)
    assert report.release_mode is True


def test_validator_timeout(fake_corpus: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANDA_FAKE_SLEEP", "5000")
    with pytest.raises(ExecutionTimeout):
        Validator(fake_corpus, timeout=1).run()


def test_validator_missing_script(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Validator(tmp_path).run()


def test_validator_rejects_old_node(tmp_path: Path, fake_corpus: Path) -> None:
    validator = Validator(fake_corpus, min_major=99)
    with pytest.raises(Exception) as excinfo:
        validator.run()
    assert "too old" in str(excinfo.value) or "require" in str(excinfo.value)


def test_validator_node_missing(fake_corpus: Path) -> None:
    validator = Validator(fake_corpus, node="definitely-not-node-xyz")
    with pytest.raises(Exception) as excinfo:
        validator.run()
    assert "Node.js" in str(excinfo.value)
