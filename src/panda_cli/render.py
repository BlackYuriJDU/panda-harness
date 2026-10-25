"""CLI rendering helpers. All user-facing strings are English in v1."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from panda_cli.brand import PALETTE, brand_lines, color_enabled

GREEN = PALETTE["green"]
GREEN_DIM = PALETTE["green.dim"]
AMBER = PALETTE["amber"]
AMBER_DIM = PALETTE["amber.dim"]
WHITE = PALETTE["white"]
WHITE_SHADOW = PALETTE["white.shadow"]
INK = PALETTE["ink"]


def console() -> Console:
    return Console(highlight=False, emoji=False, soft_wrap=False)


def banner(version: str) -> None:
    output = console()
    if color_enabled():
        output.print(Group(*brand_lines(version)))
        output.print()
    else:
        output.print(f"PANDA {version}")
        output.print()


def status_text(status: str) -> Text:
    mapping = {
        "ok": ("✓", GREEN),
        "pass": ("✓", GREEN),
        "warn": ("!", AMBER),
        "fail": ("×", AMBER),
        "denied": ("×", AMBER),
        "allowed": ("✓", GREEN),
        "skipped": ("–", WHITE_SHADOW),
    }
    glyph, color = mapping.get(status, ("·", WHITE_SHADOW))
    return Text(glyph, style=f"bold {color}")


def print_error(exc: Any) -> None:
    output = Console(stderr=True, highlight=False, emoji=False, soft_wrap=False)
    output.print(Text("error", style=f"bold {AMBER}"), Text(str(getattr(exc, "message", exc))))
    hint = getattr(exc, "hint", None)
    if hint:
        output.print(Text("hint  ", style=WHITE_SHADOW), Text(hint, style=WHITE_SHADOW))
    details = getattr(exc, "details", None)
    if details:
        output.print(Text(json.dumps(details, ensure_ascii=False, indent=2), style=WHITE_SHADOW))


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def validation_report(report: Any) -> None:
    output = console()
    mode = "release" if report.release_mode else "default"
    output.print(
        Text("panda validate", style=f"bold {WHITE}"),
        Text(f"  {report.root}", style=WHITE_SHADOW),
        Text(f"  [{mode}]", style=GREEN_DIM),
    )
    table = Table(show_header=True, header_style=f"bold {WHITE_SHADOW}", box=None, pad_edge=False)
    table.add_column("Check", style=WHITE)
    table.add_column("", width=2)
    table.add_column("Detail", style=WHITE_SHADOW, overflow="fold")
    for check in report.checks:
        if check.skipped:
            status = "skipped"
            detail = check.reason or "skipped"
        elif check.ok:
            status = "pass"
            detail = "ok"
        else:
            status = "fail"
            parts = list(check.errors)
            if check.warnings:
                parts.extend(f"warning: {warning}" for warning in check.warnings)
            detail = " | ".join(parts) if parts else "failed"
        table.add_row(check.name, status_text(status), detail)
    output.print(table)
    summary = (
        f"{len(report.checks)} checks: {report.passed} passed, "
        f"{report.failed} failed, {report.skipped} skipped · {report.duration_ms} ms"
    )
    output.print(Text(summary, style=GREEN if report.ok else AMBER))
    warnings = list(report.warnings)
    if warnings:
        output.print(Text("validator warnings:", style=f"bold {AMBER}"))
        for warning in warnings:
            output.print(Text(f"  - {warning}", style=WHITE_SHADOW))
    if report.stderr:
        output.print(Text("validator stderr:", style=f"bold {AMBER}"))
        output.print(Text(report.stderr, style=WHITE_SHADOW))


def doctor_report(report: Any) -> None:
    output = console()
    table = Table(show_header=True, header_style=f"bold {WHITE_SHADOW}", box=None, pad_edge=False)
    table.add_column("", width=2)
    table.add_column("Check", style=WHITE)
    table.add_column("Detail", style=WHITE_SHADOW, overflow="fold")
    for check in report.checks:
        label = Text(check.label)
        if check.required:
            label.append(" *", style=AMBER_DIM)
        table.add_row(status_text(check.status), label, check.detail or "")
    output.print(table)
    if report.ok:
        output.print(
            Text(
                f"ready · {report.warnings} warning(s), {report.failures} failure(s)",
                style=GREEN,
            )
        )
    else:
        output.print(
            Text(
                f"not ready · {report.warnings} warning(s), {report.failures} failure(s)",
                style=AMBER,
            )
        )
    hints = [check for check in report.checks if check.hint and check.status != "ok"]
    if hints:
        output.print()
        for check in hints:
            output.print(Text(f"{check.id}: ", style=AMBER), Text(check.hint, style=WHITE_SHADOW))


def sessions_table(summaries: Iterable[Any]) -> None:
    output = console()
    table = Table(show_header=True, header_style=f"bold {WHITE_SHADOW}", box=None, pad_edge=False)
    table.add_column("Session", style=WHITE)
    table.add_column("Started", style=WHITE_SHADOW)
    table.add_column("Events", justify="right")
    table.add_column("", width=2)
    table.add_column("Label", style=WHITE_SHADOW, overflow="fold")
    count = 0
    for summary in summaries:
        count += 1
        table.add_row(
            summary.session_id,
            summary.started_at or "",
            str(summary.events),
            status_text("pass" if summary.closed else "warn"),
            summary.label,
        )
    if count == 0:
        output.print(Text("no sessions yet", style=WHITE_SHADOW))
    else:
        output.print(table)


def session_verification(verification: Any) -> None:
    output = console()
    output.print(
        Text(verification.session_id, style=f"bold {WHITE}"),
        Text(f"  {verification.count} events", style=WHITE_SHADOW),
    )
    if verification.ok:
        output.print(Text("chain intact", style=GREEN))
    else:
        output.print(Text("chain invalid", style=AMBER))
        for issue in verification.errors:
            location = f"seq {issue.seq}: " if issue.seq is not None else ""
            output.print(Text(f"  - {location}{issue.code}: {issue.message}", style=WHITE_SHADOW))


def session_detail(summary: Any, verification: Any, events: Iterable[Any]) -> None:
    output = console()
    output.print(
        Text(summary.session_id, style=f"bold {WHITE}"),
        Text(f"  {summary.label}", style=WHITE_SHADOW),
    )
    output.print(
        Text.assemble(
            ("started ", WHITE_SHADOW),
            (summary.started_at or "-", WHITE),
            ("  events ", WHITE_SHADOW),
            (str(summary.events), WHITE),
            ("  ", ""),
            ("closed" if summary.closed else "open", GREEN if summary.closed else AMBER),
        )
    )
    session_verification(verification)
    table = Table(show_header=True, header_style=f"bold {WHITE_SHADOW}", box=None, pad_edge=False)
    table.add_column("Seq", justify="right", style=WHITE_SHADOW)
    table.add_column("Time", style=WHITE_SHADOW)
    table.add_column("Type", style=WHITE)
    table.add_column("Data", style=WHITE_SHADOW, overflow="fold")
    for event in events:
        snippet = json.dumps(event.data, ensure_ascii=False)
        if len(snippet) > 180:
            snippet = snippet[:177] + "..."
        table.add_row(str(event.seq), event.ts, event.type, snippet)
    output.print(table)


def rules_table(rules: Iterable[Any], decision: Any | None = None) -> None:
    output = console()
    table = Table(show_header=True, header_style=f"bold {WHITE_SHADOW}", box=None, pad_edge=False)
    table.add_column("Rule", style=WHITE)
    table.add_column("Effect", style=WHITE)
    table.add_column("Risk", style=WHITE_SHADOW)
    table.add_column("Actions", style=WHITE_SHADOW)
    table.add_column("Resources", style=WHITE_SHADOW, overflow="fold")
    for rule in rules:
        table.add_row(
            rule.id,
            rule.effect,
            rule.risk,
            ", ".join(rule.actions),
            ", ".join(rule.resources),
        )
    output.print(table)
    if decision is not None:
        decision_panel(decision, matched=True)


def verify_table(verifications: Iterable[Any]) -> None:
    output = console()
    table = Table(show_header=True, header_style=f"bold {WHITE_SHADOW}", box=None, pad_edge=False)
    table.add_column("Session", style=WHITE)
    table.add_column("Events", justify="right")
    table.add_column("", width=2)
    table.add_column("Issues", style=WHITE_SHADOW, overflow="fold")
    any_fail = False
    for verification in verifications:
        if not verification.ok:
            any_fail = True
        issues = "; ".join(f"{issue.code}@{issue.seq or '-'}" for issue in verification.errors)
        table.add_row(
            verification.session_id,
            str(verification.count),
            status_text("pass" if verification.ok else "fail"),
            issues or "ok",
        )
    output.print(table)
    output.print(
        Text(
            "all sessions verified" if not any_fail else "verification failed",
            style=GREEN if not any_fail else AMBER,
        )
    )


def study_table(studies: Iterable[Any]) -> None:
    output = console()
    table = Table(show_header=True, header_style=f"bold {WHITE_SHADOW}", box=None, pad_edge=False)
    table.add_column("Study", style=WHITE)
    table.add_column("Track", style=GREEN_DIM)
    table.add_column("Status", style=WHITE_SHADOW)
    table.add_column("License", style=WHITE_SHADOW, overflow="fold")
    table.add_column("Question", style=WHITE_SHADOW, overflow="fold")
    count = 0
    for study in studies:
        count += 1
        table.add_row(study.id, study.track, study.status, study.license[:40], study.question[:60])
    output.print(table if count else Text("no studies matched", style=WHITE_SHADOW))


def study_detail(study: Any, card: dict[str, Any] | None = None) -> None:
    output = console()
    output.print(Text(study.id, style=f"bold {WHITE}"), Text(f"  {study.track}", style=GREEN))
    rows = [
        ("status", study.status),
        ("question", study.question),
        ("data", study.data),
        ("license", study.license),
        ("next", study.next),
        ("card", study.path),
    ]
    for key, value in rows:
        if value:
            output.print(Text(f"{key:>9}  ", style=WHITE_SHADOW), Text(str(value), style=WHITE))
    if card and card.get("body"):
        output.print()
        output.print(Text(card["body"], style=WHITE_SHADOW))


def decision_panel(decision: Any, *, matched: bool = False) -> None:
    output = console()
    effect = decision.effect
    color = GREEN if effect == "allow" else AMBER
    lines = [
        Text.assemble(("action  ", WHITE_SHADOW), (decision.action, WHITE)),
        Text.assemble(("resource", WHITE_SHADOW), (" ", ""), (decision.resource, WHITE)),
        Text.assemble(("effect  ", WHITE_SHADOW), (effect, f"bold {color}")),
        Text.assemble(
            ("risk    ", WHITE_SHADOW),
            (decision.risk, AMBER if decision.risk in {"high", "critical"} else WHITE),
        ),
        Text.assemble(("rule    ", WHITE_SHADOW), (decision.rule_id or "-", WHITE)),
    ]
    if decision.reason:
        lines.append(Text.assemble(("reason  ", WHITE_SHADOW), (decision.reason, WHITE_SHADOW)))
    output.print(Panel(Group(*lines), border_style=color, title="policy", title_align="left"))


def init_panel(result: Any) -> None:
    output = console()
    title = "panda init (dry run)" if result.dry_run else "panda init"
    lines: list[Text] = [Text.assemble(("root  ", WHITE_SHADOW), (str(result.root), WHITE))]
    for label, items in (
        ("created", result.created),
        ("updated", result.updated),
        ("kept", result.unchanged),
    ):
        for item in items:
            lines.append(Text.assemble((f"{label:>8}  ", WHITE_SHADOW), (item, WHITE)))
    if result.opencode_path:
        lines.append(
            Text.assemble(
                ("opencode", WHITE_SHADOW),
                ("  ", ""),
                (str(result.opencode_path), WHITE),
            )
        )
    for change in result.opencode_changes:
        lines.append(Text.assemble(("  change  ", GREEN_DIM), (change, WHITE_SHADOW)))
    for warning in result.warnings:
        lines.append(Text.assemble(("  warn    ", AMBER), (warning, WHITE_SHADOW)))
    output.print(Panel(Group(*lines), border_style=GREEN, title=title, title_align="left"))
    output.print(
        Text(
            'next: panda doctor · panda validate · panda run "<task>"',
            style=WHITE_SHADOW,
        )
    )


def status_panel(info: dict[str, Any], last_session: Any = None) -> None:
    output = console()
    root = info.get("root", "")
    model = info.get("model", "")
    lines: list[Text] = [
        Text.assemble(("root   ", WHITE_SHADOW), (str(root), WHITE)),
        Text.assemble(("model  ", WHITE_SHADOW), (str(model), GREEN)),
        Text.assemble(
            ("validator", WHITE_SHADOW),
            ("  ", ""),
            (
                str(info.get("validator", "")),
                WHITE if info.get("validator_exists") else AMBER,
            ),
        ),
        Text.assemble(
            ("opencode", WHITE_SHADOW),
            ("  ", ""),
            (
                "wired" if info.get("opencode", {}).get("ok") else "not wired",
                GREEN if info.get("opencode", {}).get("ok") else AMBER,
            ),
        ),
    ]
    tools = info.get("tools", {})
    lines.append(
        Text.assemble(
            ("tools  ", WHITE_SHADOW),
            (
                f"python {tools.get('python', '?')} · node {tools.get('node') or 'missing'} · "
                f"opencode {tools.get('opencode') or 'missing'}",
                WHITE_SHADOW,
            ),
        )
    )
    if last_session:
        lines.append(
            Text.assemble(
                ("session", WHITE_SHADOW),
                (" ", ""),
                (
                    f"{last_session.session_id} · {last_session.events} events · "
                    f"{'closed' if last_session.closed else 'open'}",
                    WHITE_SHADOW,
                ),
            )
        )
    output.print(Panel(Group(*lines), border_style=GREEN, title="panda", title_align="left"))
    output.print(
        Text(
            "panda doctor · panda validate · panda study list · panda run \"<task>\"",
            style=WHITE_SHADOW,
        )
    )
