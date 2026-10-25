"""Adapter for the corpus validator (`corpus/validation/validate.mjs`).

Panda does not reimplement corpus rules. It invokes the canonical Node
validator, parses its JSON contract, and maps the result to stable exit codes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panda_core.errors import ConfigError, DependencyError, ExecutionTimeout, ProtocolError
from panda_core.process import run_command, split_command
from panda_core.version import __version__

CHECK_NAMES = (
    "schema",
    "study",
    "track",
    "evidence",
    "numbers",
    "claims",
    "findings",
    "license",
    "secrets",
    "public-face",
)

UNCONDITIONAL_CHECKS = CHECK_NAMES[:-1]


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    skipped: bool = False
    reason: str = ""
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "skipped": self.skipped,
            "reason": self.reason,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    generated_at: str
    root: str
    release_mode: bool
    checks: tuple[CheckResult, ...]
    exit_code: int
    duration_ms: int
    node_version: str | None = None
    stderr: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> int:
        return sum(1 for check in self.checks if check.ok and not check.skipped)

    @property
    def failed(self) -> int:
        return sum(1 for check in self.checks if not check.ok and not check.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for check in self.checks if check.skipped)

    def check(self, name: str) -> CheckResult | None:
        for item in self.checks:
            if item.name == name:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "generated_at": self.generated_at,
            "root": self.root,
            "release_mode": self.release_mode,
            "exit_code": self.exit_code,
            "checks": [check.to_dict() for check in self.checks],
            "summary": {
                "passed": self.passed,
                "failed": self.failed,
                "skipped": self.skipped,
                "total": len(self.checks),
            },
            "panda": {
                "version": __version__,
                "duration_ms": self.duration_ms,
                "node_version": self.node_version,
                "stderr": self.stderr,
                "warnings": list(self.warnings),
            },
        }


class Validator:
    def __init__(
        self,
        root: Path,
        *,
        script: Path | str | None = None,
        node: str | Sequence[str] = "node",
        min_major: int = 20,
        timeout: int = 180,
        check_node: bool = True,
    ) -> None:
        self.root = root.resolve()
        self.script = Path(script) if script else self.root / "corpus/validation/validate.mjs"
        self.node = split_command(node)
        self.min_major = min_major
        self.timeout = timeout
        self.check_node = check_node
        self._node_version: str | None = None

    def node_version(self) -> str:
        if self._node_version is not None:
            return self._node_version
        if not self.node:
            raise DependencyError("Node.js is not configured")
        result = run_command([*self.node, "--version"], timeout=10)
        if not result.ok:
            raise DependencyError(
                f"cannot execute Node.js: {' '.join(self.node)}",
                hint="install Node.js 20+ and ensure it is on PATH",
                details={"stderr": result.stderr.strip()},
            )
        import re

        match = re.search(r"(\d+)\.", result.stdout or result.stderr)
        if not match:
            raise DependencyError(
                f"cannot parse Node.js version output: {(result.stdout or result.stderr).strip()}"
            )
        major = int(match.group(1))
        if major < self.min_major:
            raise DependencyError(
                f"Node.js {major} is too old; require {self.min_major}+",
                hint="the corpus validator uses modern Node APIs; upgrade Node.js",
            )
        self._node_version = (result.stdout or result.stderr).strip()
        return self._node_version

    def run(self, *, release: bool = False) -> ValidationReport:
        if not self.script.is_file():
            raise ConfigError(
                f"validator not found: {self.script}",
                hint="pass --validator or run `panda init` in the corpus root",
            )
        node_version = self.node_version() if self.check_node else None
        command = [*self.node, str(self.script)]
        if release:
            command.append("--release")

        started = time.monotonic()
        result = run_command(command, cwd=self.root, timeout=self.timeout)
        duration_ms = int((time.monotonic() - started) * 1000)
        if result.timed_out:
            raise ExecutionTimeout(
                f"validator exceeded {self.timeout}s",
                hint="increase --timeout or inspect the corpus for a hang",
            )
        if result.returncode not in (0, 1):
            raise ProtocolError(
                f"validator exited with code {result.returncode}",
                hint="the validator crashed before producing a JSON report",
                details={
                    "stderr": result.stderr.strip()[-4000:],
                    "stdout": result.stdout.strip()[-4000:],
                },
            )
        report = parse_report(result.stdout)
        if result.returncode == 0 and not report.ok:
            raise ProtocolError("validator reported failure but exited 0")
        if result.returncode == 1 and report.ok:
            raise ProtocolError("validator reported success but exited 1")
        return ValidationReport(
            ok=report.ok,
            generated_at=report.generated_at,
            root=report.root,
            release_mode=report.release_mode,
            checks=report.checks,
            exit_code=result.returncode,
            duration_ms=duration_ms,
            node_version=node_version,
            stderr=result.stderr.strip()[-4000:],
            warnings=report.warnings,
        )


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProtocolError(f"validator field {field_name} must be a list")
    return tuple(str(item) for item in value)


def parse_report(stdout: str) -> ValidationReport:
    """Parse the validator's JSON contract without reimplementing its checks."""

    text = stdout.strip()
    if not text:
        raise ProtocolError("validator produced no output")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        # Be tolerant of a warning line before the JSON object, but not of
        # arbitrary output: the report must be the last JSON value.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ProtocolError("validator output is not JSON") from None
        try:
            raw = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"invalid validator JSON: {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise ProtocolError("validator report must be a JSON object")
    checks_raw = raw.get("checks")
    if not isinstance(checks_raw, list):
        raise ProtocolError("validator report is missing a checks array")

    checks: list[CheckResult] = []
    for index, item in enumerate(checks_raw):
        if not isinstance(item, dict):
            raise ProtocolError(f"check {index} is not an object")
        try:
            name = str(item["name"])
            ok = bool(item["ok"])
        except KeyError as exc:
            raise ProtocolError(f"check {index} is missing {exc.args[0]}") from exc
        checks.append(
            CheckResult(
                name=name,
                ok=ok,
                skipped=bool(item.get("skipped", False)),
                reason=str(item.get("reason", "")),
                errors=_string_list(item.get("errors"), f"checks[{index}].errors"),
                warnings=_string_list(item.get("warnings"), f"checks[{index}].warnings"),
            )
        )

    ok = bool(raw.get("ok"))
    computed_ok = all(check.ok or check.skipped for check in checks)
    if ok != computed_ok:
        raise ProtocolError(
            "validator report is inconsistent: top-level ok does not match checks",
            details={"ok": ok, "computed_ok": computed_ok},
        )

    warnings: list[str] = []
    names = [check.name for check in checks]
    missing = [name for name in CHECK_NAMES if name not in names]
    extra = [name for name in names if name not in CHECK_NAMES]
    if missing:
        warnings.append("validator checks missing from report: " + ", ".join(missing))
    if extra:
        warnings.append("unexpected validator checks: " + ", ".join(extra))
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        warnings.append("duplicate validator checks: " + ", ".join(duplicates))

    generated_at = str(raw.get("generated_at", ""))
    root = str(raw.get("root", ""))
    release_mode = bool(raw.get("release_mode", False))
    if not generated_at:
        warnings.append("validator report has no generated_at")
    if not root:
        warnings.append("validator report has no root")

    return ValidationReport(
        ok=ok,
        generated_at=generated_at,
        root=root,
        release_mode=release_mode,
        checks=tuple(checks),
        exit_code=0 if ok else 1,
        duration_ms=0,
        warnings=tuple(warnings),
    )
